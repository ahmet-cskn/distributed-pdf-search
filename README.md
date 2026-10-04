# scalable-pdf-manager

[![CI](https://github.com/ahmet-cskn/scalable-pdf-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmet-cskn/scalable-pdf-manager/actions/workflows/ci.yml)

Distributed PDF indexing and substring search.

Drop a batch of PDFs into a folder: they are uploaded to Amazon S3, queued through Amazon SQS, and processed by an autoscaling pool of Kubernetes workers that extract each page's text into a Redis trigram index. A query service then returns every `(file, page)` whose text contains a given string.

**Stack:** Python · Amazon S3 · Amazon SQS · Redis · Kubernetes (kind) · KEDA · Terraform · FastAPI · Prometheus

> Work in progress. See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Run locally

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
# Start Redis
docker compose up -d --wait

# Install dependencies (uv also installs the right Python version)
uv sync

# Index a folder of PDFs (single process)
uv run pdfsearch-index path/to/pdfs

# Start the query service, then open http://localhost:8000
uv run uvicorn pdfsearch.api:app --reload
```

The API is documented interactively at http://localhost:8000/docs.

| Endpoint | Description |
|---|---|
| `GET /` | Search page |
| `GET /search?q=<query>` | Pages containing the query (`400` if shorter than 3 characters after normalization) |
| `GET /status` | Number of files processing, done and failed |

## AWS infrastructure

The S3 bucket, SQS queues, IAM users and budget alert are defined with Terraform in [`infra/`](infra/). Requirements: the [AWS CLI](https://aws.amazon.com/cli/) and [Terraform](https://developer.hashicorp.com/terraform/install).

```bash
aws login --profile <your-profile>      # temporary credentials, no stored keys
export AWS_PROFILE=<your-profile>

cd infra
cp terraform.tfvars.example terraform.tfvars   # then set your alert email
terraform init
terraform apply
```

`terraform output` prints the bucket name and queue URLs. Terraform state stays local and is git-ignored.

## Run the pipeline against AWS

Each component has its own least-privilege IAM user. Create access keys for the worker and the watcher once and store them as CLI profiles, without printing the secrets (zsh):

```bash
for C in worker watcher; do
  aws iam create-access-key --user-name pdfsearch-$C --profile <your-profile> \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text \
    | read -r KEY SECRET \
    && aws configure set aws_access_key_id "$KEY" --profile pdfsearch-$C \
    && aws configure set aws_secret_access_key "$SECRET" --profile pdfsearch-$C \
    && aws configure set region eu-north-1 --profile pdfsearch-$C
done; unset KEY SECRET
```

Then run each component in its own terminal (Redis must be running):

```bash
# 1. Query service: http://localhost:8000
uv run uvicorn pdfsearch.api:app

# 2. Worker: indexes uploaded PDFs
AWS_PROFILE=pdfsearch-worker \
QUEUE_URL=$(terraform -chdir=infra output -raw queue_url) \
uv run pdfsearch-worker

# 3. Watcher: uploads PDFs dropped into inbox/ (git-ignored)
mkdir -p inbox
AWS_PROFILE=pdfsearch-watcher \
uv run pdfsearch-watch inbox --bucket $(terraform -chdir=infra output -raw bucket_name)
```

Copy PDFs into `inbox/`; they become searchable within seconds. The watcher uploads each file once (files already in the bucket are skipped, also after a restart), ignores subfolders and hidden files, and only accepts names ending in `.pdf` or `.PDF`.

Worker and watcher stop gracefully on Ctrl+C (press twice to stop immediately). See `pdfsearch.worker.main` and `pdfsearch-watch --help` for all settings.

## Run on Kubernetes with autoscaling

Redis, the query service and the workers run in a local [kind](https://kind.sigs.k8s.io/) cluster; [KEDA](https://keda.sh/) scales the workers between 0 and 8 based on the SQS queue length. The watcher stays on the host. Requirements: kind, kubectl, Helm, and the AWS setup and CLI profiles from the previous sections (including one for `pdfsearch-keda`).

```bash
make cluster          # create the kind cluster
make monitoring       # install Prometheus
make keda             # install KEDA
make image            # build the Docker image and load it into the cluster
make worker-secret    # AWS keys from the pdfsearch-worker / pdfsearch-keda
make keda-secret      #   profiles, stored as Kubernetes Secrets
make deploy           # Redis, API, worker and the scaling rule
```

The search page is at http://localhost:8080. Start the watcher as above, then drop a large batch of generated PDFs into the inbox and watch the workers scale up, and back to zero once the queue is empty:

```bash
kubectl --context kind-pdfsearch -n pdfsearch get pods -l app=worker -w

uv run pdfsearch-generate inbox --count 1000 --prefix batch-
```

`make help` lists all targets; `make restart` rebuilds the image and rolls out new pods after a code change; `make prometheus` opens Prometheus at http://localhost:9090; [docs/METRICS.md](docs/METRICS.md) has ready-made queries.

## Development

```bash
uv run pytest           # tests (need Redis running)
uv run ruff check .     # lint
uv run ruff format .    # format
```

Tests use Redis database 15 and clear it on every run; indexed data lives in database 0.

## License

[GNU Affero General Public License v3.0](LICENSE)
