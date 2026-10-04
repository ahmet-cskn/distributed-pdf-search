# Metrics and queries

Prometheus scrapes the workers, the query service, Redis, KEDA and the kubelet (see [DESIGN.md §7](DESIGN.md#7-observability)). Open its UI with `make prometheus` (http://localhost:9090), paste a query into the **Graph** tab, and pick a time range.

`rate(x[1m])` is the per-second increase of a counter `x`, averaged over the last minute. Shorter windows react faster but are noisier; with the 5 s scrape interval, `[1m]` averages 12 samples.

## Throughput

Pages indexed per second, all workers together (the benchmark's main number):

```promql
sum(rate(pdfsearch_worker_pages_indexed_total[1m]))
```

The same per worker pod; if adding workers does not raise the total, this shows each worker slowing down:

```promql
sum by (pod) (rate(pdfsearch_worker_pages_indexed_total[1m]))
```

Files per second:

```promql
sum(rate(pdfsearch_worker_files_indexed_total[1m]))
```

## Workers and queue

Running workers (each worker pod reports one `busy` series):

```promql
count(pdfsearch_worker_busy)
```

Average worker utilization, 0–1: the fraction of time workers are processing a job rather than waiting for one. It is sampled every 5 s, so it is an estimate; values near 1 under load mean workers are never starved of jobs:

```promql
avg(avg_over_time(pdfsearch_worker_busy[1m]))
```

Queue length as KEDA sees it (waiting plus in-flight messages), which it scales on:

```promql
keda_scaler_metrics_value{scaledObject="worker"}
```

Job outcomes per second (`done`, `skipped`, `retry`, `failed`, `invalid`, `error`):

```promql
sum by (outcome) (rate(pdfsearch_worker_jobs_total[5m]))
```

## Where workers spend their time

The central view for finding the bottleneck. Worker-seconds spent per second in each phase: summed over all workers, so with N busy workers the phases add up to about N. The largest phase is where the time goes:

```promql
sum by (phase) (rate(pdfsearch_worker_phase_seconds_sum[1m]))
```

Average duration of one job's phase:

```promql
sum by (phase) (rate(pdfsearch_worker_phase_seconds_sum[1m]))
  / sum by (phase) (rate(pdfsearch_worker_phase_seconds_count[1m]))
```

95th percentile per phase (computed from histogram buckets, so it is accurate only to the nearest bucket boundary):

```promql
histogram_quantile(0.95, sum by (phase, le) (rate(pdfsearch_worker_phase_seconds_bucket[5m])))
```

Extraction and Redis time **per page**, independent of how many pages the PDFs have. If extraction per page grows as workers are added, they compete for CPU; if indexing per page grows, they wait on Redis:

```promql
sum(rate(pdfsearch_worker_phase_seconds_sum{phase="extract"}[1m]))
  / sum(rate(pdfsearch_worker_pages_indexed_total[1m]))
```

```promql
sum(rate(pdfsearch_worker_phase_seconds_sum{phase="index"}[1m]))
  / sum(rate(pdfsearch_worker_pages_indexed_total[1m]))
```

## Redis

Redis CPU in cores. Redis executes commands on a single thread, so a value close to 1 means Redis itself is saturated, and more workers cannot write faster:

```promql
rate(redis_cpu_user_seconds_total[1m]) + rate(redis_cpu_sys_seconds_total[1m])
```

Commands per second:

```promql
rate(redis_commands_processed_total[1m])
```

Memory used:

```promql
redis_memory_used_bytes
```

## CPU per container

CPU cores used per container in the pdfsearch namespace (from the kubelet):

```promql
sum by (pod, container) (rate(container_cpu_usage_seconds_total{namespace="pdfsearch", container!=""}[1m]))
```

All worker containers together:

```promql
sum(rate(container_cpu_usage_seconds_total{namespace="pdfsearch", container="worker"}[1m]))
```

All containers in the cluster, including Kubernetes itself and Prometheus. The machine has 8 cores; values near 8 mean everything is competing for CPU:

```promql
sum(rate(container_cpu_usage_seconds_total{container!=""}[1m]))
```

## Query service

Requests per second by route and status:

```promql
sum by (route, status) (rate(pdfsearch_api_requests_total[5m]))
```

95th percentile latency by route:

```promql
histogram_quantile(0.95, sum by (route, le) (rate(pdfsearch_api_request_seconds_bucket[5m])))
```

False-positive rate of the trigram filter: the share of candidate pages that turned out not to contain the query and were dropped by verification:

```promql
1 - sum(rate(pdfsearch_search_matches_sum[1h])) / sum(rate(pdfsearch_search_candidates_sum[1h]))
```

Average search time per phase (`intersect` is `SINTER`, `verify` is `MGET` plus the substring check):

```promql
sum by (phase) (rate(pdfsearch_search_phase_seconds_sum[5m]))
  / sum by (phase) (rate(pdfsearch_search_phase_seconds_count[5m]))
```

## Reading the bottleneck

During a run with many workers, compare these:

| Observation | Likely bottleneck |
|---|---|
| Redis CPU close to 1 core, `index` the largest phase, index time per page growing with more workers | Redis (single-threaded writes) |
| All-container CPU close to 8 cores, extraction time per page growing with more workers | CPU of the machine |
| `download` and `receive` large, CPU and Redis not saturated | Network round trips to AWS |
| Worker utilization well below 1 | Not enough jobs reaching the workers (queue empty, or scaling too slow) |
