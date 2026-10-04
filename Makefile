# Local Kubernetes workflow. Run `make help` for an overview.

CLUSTER   := pdfsearch
NAMESPACE := pdfsearch
IMAGE     := pdfsearch:dev
KEDA_VERSION := 2.21.0
# Always target the kind cluster explicitly, never whatever kubectl's current
# context happens to be (it could be another cluster).
KUBECTL   := kubectl --context kind-$(CLUSTER)

.PHONY: help cluster cluster-delete image keda deploy restart worker-secret keda-secret status

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

cluster: ## Create the local kind cluster (if it does not exist yet)
	@kind get clusters | grep -qx $(CLUSTER) \
		&& echo "cluster $(CLUSTER) already exists" \
		|| kind create cluster --config k8s/kind-config.yaml

cluster-delete: ## Delete the kind cluster and everything in it
	kind delete cluster --name $(CLUSTER)

image: ## Build the Docker image and load it into the cluster
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)

deploy: k8s/aws.env ## Apply all Kubernetes manifests in k8s/ (needs `make keda` first)
	$(KUBECTL) apply -k k8s/
	$(KUBECTL) -n $(NAMESPACE) rollout status statefulset/redis --timeout=120s
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment/api --timeout=120s
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment/worker --timeout=120s

restart: image ## Rebuild the image and restart the pods that use it
	$(KUBECTL) -n $(NAMESPACE) rollout restart deployment/api deployment/worker
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment/api --timeout=120s
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment/worker --timeout=120s

# Regenerated whenever the Terraform state (or this Makefile) changes.
# AWS_DEFAULT_REGION, not AWS_REGION: the Python SDK only reads the former.
k8s/aws.env: infra/terraform.tfstate Makefile
	@echo "QUEUE_URL=$$(terraform -chdir=infra output -raw queue_url)" > $@
	@echo "AWS_DEFAULT_REGION=$$(terraform -chdir=infra output -raw region)" >> $@
	@echo "wrote $@"

# Stores an IAM user's access key, read from its pdfsearch-<component> CLI
# profile, as the Kubernetes Secret <component>-aws. The secret is piped
# straight into kubectl: never printed or passed as a visible command
# argument. Safe to re-run, e.g. after rotating the key.
define aws_secret
	@printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' \
		"$$(aws configure get aws_access_key_id --profile pdfsearch-$(1))" \
		"$$(aws configure get aws_secret_access_key --profile pdfsearch-$(1))" \
	| $(KUBECTL) -n $(NAMESPACE) create secret generic $(1)-aws \
		--from-env-file=/dev/stdin --dry-run=client -o yaml \
	| $(KUBECTL) apply -f -
endef

worker-secret: ## Store the pdfsearch-worker AWS key as a Kubernetes Secret
	$(call aws_secret,worker)

keda-secret: ## Store the pdfsearch-keda AWS key as a Kubernetes Secret
	$(call aws_secret,keda)

keda: ## Install KEDA (the autoscaler) into the cluster
	helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
	helm upgrade --install keda kedacore/keda --version $(KEDA_VERSION) \
		--kube-context kind-$(CLUSTER) --namespace keda --create-namespace --wait

status: ## Show the pods in the pdfsearch namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide
