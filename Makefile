# Local Kubernetes workflow. Run `make help` for an overview.

CLUSTER   := pdfsearch
NAMESPACE := pdfsearch
IMAGE     := pdfsearch:dev
# Always target the kind cluster explicitly, never whatever kubectl's current
# context happens to be (it could be another cluster).
KUBECTL   := kubectl --context kind-$(CLUSTER)

.PHONY: help cluster cluster-delete image deploy status

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

deploy: ## Apply all Kubernetes manifests in k8s/
	$(KUBECTL) apply -k k8s/
	$(KUBECTL) -n $(NAMESPACE) rollout status statefulset/redis --timeout=120s

status: ## Show the pods in the pdfsearch namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide
