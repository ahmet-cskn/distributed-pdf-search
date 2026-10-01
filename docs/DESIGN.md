# Design Document

## 1. Overview

**scalable-pdf-manager** indexes large batches of PDF files and answers substring queries over their text.

A user drops PDFs (potentially thousands) into a folder. The files are uploaded to Amazon S3, which emits an event per file into an Amazon SQS queue. A horizontally autoscaled pool of worker pods on Kubernetes consumes the queue, extracts the text of every page, and writes it into a trigram index in Redis. A query service answers: *"which (file, page) pairs contain this string?"*

### 1.1 Goals

- Return **exactly** the set of `(file, page)` pairs whose normalized text contains the normalized query as a substring.
- Ingest large batches in parallel; throughput scales with the number of workers.
- No file is lost when a worker crashes; reprocessing a file is harmless.
- Infrastructure is reproducible (Terraform) and runs at near-zero cost.

### 1.2 Non-goals (current scope)

- Matches that span a page boundary.
- OCR for scanned PDFs (pages without a text layer are indexed as empty).
- Ligature handling, re-joining words hyphenated across line breaks.
- Languages other than English; non-ASCII characters are discarded.
- Result snippets or match positions.
- Updating or deleting files once indexed.
- Pagination or result caps.

## 2. Architecture

```mermaid
flowchart LR
    U[User folder] --> W[Watcher<br/>local process]
    W -- upload --> S3[(S3 bucket)]
    S3 -- ObjectCreated event --> Q[[SQS queue]]
    Q -- after 3 failed receives --> DLQ[[Dead-letter queue]]
    Q --> WK[Worker pods × N]
    WK -- download PDF --> S3
    WK -- page text + trigrams --> R[(Redis)]
    K[KEDA] -. watches queue length .-> Q
    K -. scales .-> WK
    B[Browser] --> API[Query service<br/>FastAPI]
    API --> R
```

| Component | Runs on | Responsibility |
|---|---|---|
| Watcher | Laptop (plain Python process) | Detect new PDFs in the folder and upload them to S3 |
| S3 bucket | AWS (`eu-central-1`) | Durable storage for PDFs; emits an event per upload |
| SQS queue + DLQ | AWS | Distributes one job per file to workers; isolates poison messages |
| Workers | kind cluster (Deployment) | Download, extract, normalize, index |
| KEDA | kind cluster | Scales workers on SQS queue length (0 ↔ N) |
| Redis | kind cluster (StatefulSet, AOF) | Trigram index, page texts, file status |
| Query service | kind cluster (Deployment) | HTTP search API + minimal search page |
| Prometheus + Grafana | kind cluster | Metrics and dashboards |

## 3. Ingestion pipeline

### 3.1 Watcher

- Watches a single local folder for new `*.pdf` files.
- **Stability check:** a file is uploaded only after its size has stopped changing for ~2 s (the OS reports files before a copy completes).
- Uploads with the filename as the S3 key. Large files use multipart upload (handled by the AWS SDK).
- **Startup sync:** lists the bucket and uploads only local files that are missing, covering files dropped while the watcher was down.
- The watcher does **not** create jobs; S3 does. Any upload path (watcher, `aws s3 cp`, console) triggers indexing.
- Filenames are unique because they come from a single folder.

### 3.2 S3 → SQS

- The bucket is private (public access blocked) with default encryption.
- An event notification on `s3:ObjectCreated:*` with suffix filter `.pdf` delivers to an SQS **standard** queue (ordering is not needed, so FIFO is not used).
- Redrive policy: after **3** receives, a message moves to the DLQ (14-day retention).

### 3.3 Worker

```
loop:
    msg = sqs.receive(max_messages=1, wait_time=20s)        # long polling
    if msg is s3:TestEvent: delete, continue
    key = url_decode(msg.Records[0].s3.object.key)          # S3 URL-encodes keys
    set status(key) = processing
    pdf = s3.download(key)
    for each page (1-based):                                # visibility heartbeat runs meanwhile
        text = normalize(extract(page))
        index_page(key, page, text)                         # §4.4
    set status(key) = done, pages = n
    sqs.delete(msg)

on error:
    if msg.ApproximateReceiveCount >= 3: set status(key) = failed
    do not delete → SQS redelivers it, or moves it to the DLQ
```

- **One message per receive**, so idle workers are never starved by a busy one.
- **Visibility timeout** 5 min; a background heartbeat calls `ChangeMessageVisibility` every 60 s while a file is processing, so a long PDF is never handed to a second worker mid-processing.
- **Delete after write:** the message is deleted only after all Redis writes for the file succeed.
- **Graceful shutdown:** on `SIGTERM` the worker stops receiving, then either finishes the current file or exits without deleting (the message reappears after the visibility timeout).
- **Stateless:** all state lives in SQS, S3, or Redis, so workers can be added or removed at any time.
- Text extraction uses **PyMuPDF**.

### 3.4 Delivery semantics

Both S3 event notifications and SQS standard queues are **at-least-once**: a file may be processed more than once. Correctness relies on the index writes being idempotent (§4.4), not on exactly-once delivery.

### 3.5 Autoscaling

KEDA's `aws-sqs-queue` scaler drives the worker Deployment:

- Target: ~10 messages per worker.
- Min replicas: 0 (scale to zero when idle). Max replicas: configurable (default 20).

### 3.6 Progress tracking

Workers maintain per-file status in Redis (§4.3). Pending files are visible as the SQS queue length; failed files are visible both in Redis and in the DLQ. The query service exposes aggregate counts.

## 4. Search

### 4.1 Normalization

One shared function, applied identically to page text and to queries:

1. Lowercase.
2. Replace every character outside `[a-z0-9]` with a space.
3. Collapse runs of spaces into one.
4. Trim.

| Input | Normalized |
|---|---|
| `Hello, Everyone!` | `hello everyone` |
| `don't` | `don t` |
| `café` | `caf` |
| `C++` | `c` |

Queries whose normalized form is shorter than 3 characters are rejected.

### 4.2 Trigrams

- Sliding window of size 3 over the normalized text, **including spaces** (`"hello every"` yields `"o e"`, `" ev"`, …).
- De-duplicated into a set per page (and per query).
- Text shorter than 3 characters yields no trigrams.

### 4.3 Data model (Redis)

| Key | Type | Content |
|---|---|---|
| `page:<file>#<page>` | String | Normalized text of the page |
| `tri:<trigram>` | Set | Page ids (`<file>#<page>`) containing the trigram |
| `file:<file>` | Hash | `status` (`processing` / `done` / `failed`), `pages` |
| `files:<status>` | Set | Filenames currently in that status (for counts) |

- Page id: `<file>#<page>`, page numbers 1-based. Parsed by splitting on the **last** `#` (filenames may contain `#`).
- Status changes update `file:<file>` and move the filename between `files:<status>` sets in one `MULTI` transaction.
- Persistence: **AOF** enabled.

### 4.4 Write path (per page)

One pipelined batch:

1. `SET page:<id> <text>` — **first**.
2. `SADD tri:<t> <id>` for every trigram of the page.

Properties:

- **Concurrent writers are safe:** Redis executes commands one at a time and each command is atomic; no application-level locking.
- **Idempotent:** re-setting the same text and re-adding an existing set member are no-ops, so redelivered files cannot corrupt the index.
- **Crash-safe:** a partially written page is completed when the file is redelivered.
- **Text before trigrams:** a page can never become a search candidate without its text being available for verification.

### 4.5 Query path

1. Normalize the query; reject if shorter than 3 characters.
2. Compute the query's trigram set.
3. `SINTER tri:<t1> tri:<t2> …` → candidate pages (Redis iterates the smallest set; a missing key yields an empty result).
4. `MGET page:<id> …` → candidate texts in one round trip.
5. Keep candidates whose text contains the normalized query.
6. Sort by filename, then page number; return `[{file, page}, …]`.

**Correctness:** the trigram filter may produce false positives but never false negatives; step 5 removes the false positives, so results are exact.

### 4.6 API

| Endpoint | Description |
|---|---|
| `GET /` | Minimal HTML page with a search box |
| `GET /search?q=<query>` | `200` with `{"query": …, "results": [{"file": …, "page": …}]}`; `400` if the normalized query is shorter than 3 characters |
| `GET /status` | Counts of files per status |

## 5. Scaling phases

### 5.1 v1 — single Redis

All workers write directly to one Redis instance; one query service reads from it.

### 5.2 v2 — sharded by file

- N **independent** Redis instances; a file belongs to shard `hash(filename) % N`. All keys of a file (pages, trigram memberships, status) live on its shard.
- Workers route writes by filename.
- The query service fans out each query to all shards in parallel; each shard runs steps 3–5 of §4.5 locally; results are merged and sorted.
- **Why not Redis Cluster:** it partitions keys by key hash, i.e. by trigram. `SINTER` across keys on different nodes fails (`CROSSSLOT`), and verification would need page text from other nodes. Partitioning by document keeps every query step shard-local.
- **Limitation:** changing N requires re-indexing.
- **Motivation:** write throughput (a single Redis becomes the bottleneck with enough workers) and fault isolation — not storage size.

## 6. Infrastructure and deployment

- **AWS** (Terraform, region `eu-central-1`): S3 bucket, SQS queue, DLQ, S3→SQS notification and queue policy, IAM users and least-privilege policies, AWS Budgets alert (~$5/month).
- **IAM (least privilege):**

  | Identity | Permissions |
  |---|---|
  | watcher | `s3:PutObject`, `s3:ListBucket` on the bucket |
  | worker | `s3:GetObject` on the bucket; `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility` on the queue |
  | keda | `sqs:GetQueueAttributes` on the queue |

- **Secrets:** access keys are created with the AWS CLI (not Terraform, so they never land in Terraform state), stored as Kubernetes Secrets, and never committed. Terraform state is local and git-ignored.
- **Kubernetes:** local **kind** cluster. Plain YAML manifests for project services; KEDA, Prometheus and Grafana installed via Helm.
- **Development:** Docker Compose provides a local Redis; LocalStack emulates S3 and SQS for automated tests.

## 7. Observability

Prometheus scrapes metrics from workers and the query service; Grafana dashboards show:

- Pages and files processed per second
- Queue length (visible / in flight) and DLQ size
- Worker replica count
- Query latency

**Benchmark caveat:** all pods share one laptop, so throughput plateaus around the machine's CPU core count. This is expected and documented with the results.

## 8. Testing strategy

- **Unit tests:** `normalize` and `trigrams` (punctuation, whitespace runs, non-ASCII, short strings).
- **Brute-force equivalence:** index generated pages; for many queries, results must equal a naive substring scan over all pages.
- **Idempotency:** indexing the same file twice leaves the index unchanged.
- **Concurrency:** many concurrent writers produce the same index as a single writer.
- **Integration:** S3/SQS paths tested against LocalStack.
- **Test data:** generated PDFs with known content; real lecture slides for manual end-to-end testing.
- **CI:** GitHub Actions runs lint (ruff) and tests on every push, with Redis as a service container.

## 9. Capacity estimates

| Quantity | Estimate |
|---|---|
| PDFs | ~1,000 |
| Pages | ~30,000 |
| Normalized text | ~90 MB |
| Index size | a few hundred MB (fits in one Redis) |
| Extraction | ~10–50 ms/page → ~5–25 min on one core (the bottleneck) |
| Index writes | ~1,500 unique trigrams/page → ~45M `SADD` members, ~1 min of Redis time pipelined |

## 10. Technology choices

| Area | Choice | Alternatives considered |
|---|---|---|
| Language | Python | Go (weaker PDF libraries) |
| PDF extraction | PyMuPDF | pypdf (permissive license, slower, less accurate) |
| Index store | Redis sets | Own index service (more code), PostgreSQL `pg_trgm` |
| File storage | Amazon S3 | Shared volume (single machine only), MinIO |
| Queue | Amazon SQS (fed by S3 events) | RabbitMQ, Redis Streams, Kafka (stream-oriented, overkill) |
| Orchestration | Kubernetes (kind) + KEDA | Docker Compose (no autoscaling), EKS (cost) |
| Infrastructure as code | Terraform | AWS CDK, console |
| API | FastAPI | — |
| Tooling | uv, ruff, pytest, LocalStack, GitHub Actions | — |

## 11. Milestones

| # | Milestone | Done when |
|---|---|---|
| 1 | Normalization, trigram and index library | Unit and brute-force equivalence tests pass against a local Redis |
| 2 | Query API + search page | A locally indexed PDF is searchable in the browser |
| 3 | Terraform: S3, SQS, DLQ, events, IAM, budget | `terraform apply` creates everything; an upload produces an SQS message |
| 4 | Worker | A manually uploaded PDF becomes searchable |
| 5 | Watcher | Dropping files into the folder makes them searchable |
| 6 | kind + KEDA | Dropping 1,000 PDFs scales workers up and back to zero |
| 7 | Prometheus + Grafana + benchmark | Throughput vs. worker count documented |
| 8 | v2: sharded Redis | Query service fans out to N shards; tests still pass |

CI is introduced with milestone 1 and extended as the project grows.

## 12. Known limitations and future work

- Re-uploading a file under an existing name is unsupported (stale index entries would remain).
- Very large PDFs limit load balancing with per-file jobs; splitting into page-range jobs is a possible improvement.
- Changing the shard count in v2 requires a full re-index.
- Possible extensions: OCR, file deletion/update, result snippets, pagination, deployment to EKS + ElastiCache.
