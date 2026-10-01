# scalable-pdf-manager

[![CI](https://github.com/ahmet-cskn/scalable-pdf-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmet-cskn/scalable-pdf-manager/actions/workflows/ci.yml)

Distributed PDF indexing and substring search.

Drop a batch of PDFs into a folder: they are uploaded to Amazon S3, queued through Amazon SQS, and processed by an autoscaling pool of Kubernetes workers that extract each page's text into a Redis trigram index. A query service then returns every `(file, page)` whose text contains a given string.

**Stack:** Python · Amazon S3 · Amazon SQS · Redis · Kubernetes (kind) · KEDA · Terraform · FastAPI · Prometheus · Grafana

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

## Development

```bash
uv run pytest           # tests (need Redis running)
uv run ruff check .     # lint
uv run ruff format .    # format
```

Tests use Redis database 15 and clear it on every run; indexed data lives in database 0.

## License

[GNU Affero General Public License v3.0](LICENSE)
