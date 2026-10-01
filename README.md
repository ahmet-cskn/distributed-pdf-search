# scalable-pdf-manager

Distributed PDF indexing and substring search.

Drop a batch of PDFs into a folder: they are uploaded to Amazon S3, queued through Amazon SQS, and processed by an autoscaling pool of Kubernetes workers that extract each page's text into a Redis trigram index. A query service then returns every `(file, page)` whose text contains a given string.

**Stack:** Python · Amazon S3 · Amazon SQS · Redis · Kubernetes (kind) · KEDA · Terraform · FastAPI · Prometheus · Grafana

> Work in progress. See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## License

[GNU Affero General Public License v3.0](LICENSE)
