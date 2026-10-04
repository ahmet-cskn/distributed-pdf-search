"""Benchmark: indexing throughput by number of workers.

For each worker count N, on the kind cluster against the real S3/SQS:

1. Hold the workers at 0 (KEDA's paused-replicas annotation) and wipe the
   cluster's index, so every run starts from the same state.
2. Generate the same PDFs (fixed seed) and upload them; S3 queues one job each.
3. Release exactly N workers at once and time how long they take to index
   everything. Upload speed and autoscaling delay are deliberately excluded.
4. Read per-phase worker time and CPU use for the run from Prometheus.

Every run is appended to docs/benchmark/runs.csv. Single runs on a shared
laptop are noisy, so docs/benchmark/results.csv and throughput.png show the
median of all runs per worker count. Autoscaling is restored at the end,
also on Ctrl+C.

Usage (cluster deployed, `aws login` not needed):
    uv run python scripts/benchmark.py --workers 1 2 3 4 5 6 --pdfs 150 --repeat 3
    uv run python scripts/benchmark.py --plot-only

Uses the pdfsearch-watcher profile to upload (it may only write PDFs) and
the pdfsearch-keda profile to read the queue length (it may only do that).
"""

import argparse
import csv
import json
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
import pymupdf

from pdfsearch.generate import generate
from pdfsearch.upload import upload_files

CONTEXT = "kind-pdfsearch"
NAMESPACE = "pdfsearch"
API = "http://localhost:8080"
PROMETHEUS_PORT = 9092
PAUSE = "autoscaling.keda.sh/paused-replicas"
PHASES = ["receive", "download", "extract", "index", "delete"]
OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmark"
RUN_FIELDS = ["run_id", "repeat"]
FIELDS = [
    "workers",
    "runs",
    "pdfs",
    "pages",
    "seconds",
    "pages_per_s",
    "speedup",
    *[f"{phase}_ms_per_page" for phase in PHASES],
    "worker_cpu_cores",
    "redis_cpu_cores",
    "cluster_cpu_cores",
]


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def kubectl(*args: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def terraform_output(name: str) -> str:
    return subprocess.run(
        ["terraform", "-chdir=infra", "output", "-raw", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def wait_until(condition, what: str, timeout: float, interval: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(interval)


# --- Cluster control ------------------------------------------------------------


def hold_workers(n: int | None) -> None:
    """Pin the worker count to n, or hand it back to autoscaling (None)."""
    if n is None:
        kubectl("annotate", "scaledobject", "worker", f"{PAUSE}-")
    else:
        kubectl("annotate", "--overwrite", "scaledobject", "worker", f"{PAUSE}={n}")


def worker_pods() -> list[str]:
    out = kubectl(
        "get",
        "pods",
        "-l",
        "app=worker",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    )
    return out.split()


def ready_workers() -> int:
    out = kubectl("get", "deployment", "worker", "-o", "jsonpath={.status.readyReplicas}")
    return int(out or 0)


def total_workers() -> int:
    out = kubectl("get", "deployment", "worker", "-o", "jsonpath={.status.replicas}")
    return int(out or 0)


def wipe_index() -> None:
    kubectl("exec", "redis-0", "-c", "redis", "--", "redis-cli", "FLUSHALL")


def files_done() -> int:
    """Files indexed so far, or -1 if the query service does not answer.

    Near the machine's limit the API can be briefly unavailable (its
    readiness probe fails); the benchmark keeps waiting instead of crashing.
    """
    try:
        with urllib.request.urlopen(f"{API}/status", timeout=5) as response:
            return json.load(response)["done"]
    except (OSError, ValueError):
        return -1


class Prometheus:
    """Port-forward to Prometheus for the duration of the benchmark."""

    def __enter__(self):
        self._process = subprocess.Popen(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "-n",
                "monitoring",
                "port-forward",
                "svc/monitoring-kube-prometheus-prometheus",
                f"{PROMETHEUS_PORT}:9090",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_until(self._ready, "Prometheus port-forward", timeout=30, interval=0.5)
        return self

    def __exit__(self, *exc):
        self._process.terminate()

    def _ready(self) -> bool:
        try:
            urllib.request.urlopen(f"http://localhost:{PROMETHEUS_PORT}/-/ready", timeout=2)
        except OSError:
            return False
        return True

    def query(self, promql: str, at: float) -> list[dict]:
        params = urllib.parse.urlencode({"query": promql, "time": at})
        url = f"http://localhost:{PROMETHEUS_PORT}/api/v1/query?{params}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.load(response)["data"]["result"]

    def scalar(self, promql: str, at: float) -> float:
        result = self.query(promql, at)
        return float(result[0]["value"][1]) if result else 0.0


# --- One run ----------------------------------------------------------------------


def run(n: int, pdfs: int, seed: int, aws: dict, prometheus: Prometheus, run_id: str) -> dict:
    log(f"--- {n} worker(s) ---")
    hold_workers(0)
    wait_until(lambda: total_workers() == 0, "workers to stop", timeout=180)
    wipe_index()

    with tempfile.TemporaryDirectory() as tmp:
        paths = generate(Path(tmp), pdfs, seed=seed, prefix=f"bench-{run_id}-w{n}-")
        pages = sum(pymupdf.open(path).page_count for path in paths)
        result = upload_files(aws["s3"], aws["bucket"], paths)
    if result.failed:
        raise RuntimeError(f"{len(result.failed)} uploads failed")
    log(f"uploaded {pdfs} PDFs ({pages} pages)")

    def queued() -> int:
        attributes = aws["sqs"].get_queue_attributes(
            QueueUrl=aws["queue_url"], AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]
        return int(attributes["ApproximateNumberOfMessages"])

    wait_until(lambda: queued() >= pdfs, "all jobs to be queued", timeout=180)

    hold_workers(n)
    wait_until(lambda: ready_workers() == n, f"{n} workers to start", timeout=180, interval=0.5)
    start = time.time()
    log(f"{n} worker(s) running")
    wait_until(lambda: files_done() >= pdfs, "indexing to finish", timeout=3600, interval=1)
    end = time.time()
    seconds = end - start
    log(f"indexed in {seconds:.1f}s ({pages / seconds:.1f} pages/s)")

    # Let Prometheus scrape the final counter values (5 s interval).
    time.sleep(12)
    at = time.time()
    # The window spans the whole run up to now: CPU numbers are collected only
    # every 15 s, so a window of just the run itself could hold too few samples.
    window = f"{int(at - start) + 5}s"
    pods = "|".join(worker_pods())
    phase_seconds = {
        r["metric"]["phase"]: float(r["value"][1])
        for r in prometheus.query(
            # Fresh pods each run, so their counters cover exactly this run.
            f'sum by (phase) (pdfsearch_worker_phase_seconds_sum{{pod=~"{pods}"}})',
            at,
        )
    }

    def cores(promql: str) -> float:
        """Average CPU cores used during the run."""
        return round(prometheus.scalar(promql, at), 2)

    def container_cpu(selector: str) -> str:
        return f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[{window}]))"

    return {
        "workers": n,
        "pdfs": pdfs,
        "pages": pages,
        "seconds": round(seconds, 1),
        "pages_per_s": round(pages / seconds, 1),
        **{
            f"{phase}_ms_per_page": round(phase_seconds.get(phase, 0.0) / pages * 1000, 2)
            for phase in PHASES
        },
        "worker_cpu_cores": cores(container_cpu('namespace="pdfsearch", container="worker"')),
        "redis_cpu_cores": cores(
            f"rate(redis_cpu_user_seconds_total[{window}])"
            f" + rate(redis_cpu_sys_seconds_total[{window}])"
        ),
        "cluster_cpu_cores": cores(container_cpu('container!=""')),
    }


# --- Output -------------------------------------------------------------------------


def append_run(row: dict, path: Path) -> None:
    new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RUN_FIELDS + FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def summarize(runs_path: Path) -> list[dict]:
    """Median of every measured field per worker count, over all runs."""
    with runs_path.open() as f:
        runs = list(csv.DictReader(f))
    numeric = [f for f in FIELDS if f not in ("workers", "runs", "speedup")]
    rows = []
    for n in sorted({int(r["workers"]) for r in runs}):
        group = [r for r in runs if int(r["workers"]) == n]
        row = {"workers": n, "runs": len(group)}
        for field in numeric:
            value = statistics.median(float(r[field]) for r in group)
            row[field] = int(value) if field in ("pdfs", "pages") else round(value, 2)
        rows.append(row)
    baseline = next((r["pages_per_s"] for r in rows if r["workers"] == 1), None)
    for row in rows:
        row["speedup"] = round(row["pages_per_s"] / baseline, 2) if baseline else ""
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# Colors: categorical slots 1-5 of the validated reference palette (light mode).
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]


def plot(rows: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    workers = [int(r["workers"]) for r in rows]
    throughput = [r["pages_per_s"] for r in rows]

    plt.rcParams.update(
        {
            "font.size": 10,
            "text.color": TEXT,
            "axes.labelcolor": TEXT_SECONDARY,
            "axes.edgecolor": GRID,
            "xtick.color": TEXT_SECONDARY,
            "ytick.color": TEXT_SECONDARY,
        }
    )
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.2), facecolor=SURFACE)
    for ax in (left, right):
        ax.set_facecolor(SURFACE)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_xticks(workers)
        ax.set_xlabel("Workers")

    # Left: throughput, with what perfect linear scaling from 1 worker would give.
    if workers[0] == 1:
        left.plot(
            workers,
            [throughput[0] * w for w in workers],
            color=TEXT_SECONDARY,
            linestyle=(0, (4, 3)),
            linewidth=1.5,
            label="Linear scaling",
        )
    left.plot(
        workers,
        throughput,
        color=SERIES[0],
        linewidth=2,
        marker="o",
        markersize=7,
        label="Measured",
    )
    for w, t in zip(workers, throughput, strict=True):
        left.annotate(
            f"{t:.0f}",
            (w, t),
            textcoords="offset points",
            xytext=(0, -16),
            ha="center",
            color=TEXT,
            fontsize=9,
        )
    left.set_ylim(bottom=0)
    left.set_ylabel("Pages per second")
    left.set_title("Indexing throughput", loc="left", fontweight="bold")
    left.legend(frameon=False, loc="upper left")

    # Right: where each page's worker time goes, per phase.
    bottom = [0.0] * len(rows)
    for phase, color in zip(PHASES, SERIES, strict=True):
        values = [r[f"{phase}_ms_per_page"] for r in rows]
        right.bar(
            workers,
            values,
            bottom=bottom,
            color=color,
            width=0.7,
            label=phase,
            edgecolor=SURFACE,
            linewidth=2,
        )
        bottom = [b + v for b, v in zip(bottom, values, strict=True)]
    right.set_ylabel("Worker time per page (ms)")
    right.set_title("Time per page, by phase", loc="left", fontweight="bold")
    handles, labels = right.get_legend_handles_labels()
    right.legend(
        handles[::-1], labels[::-1], frameon=False, loc="upper left", bbox_to_anchor=(1.01, 1)
    )

    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# --- Main -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # Up to 6: with Prometheus running, 8 workers no longer fit in this
    # machine's memory (the node starts swapping and health checks fail).
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    parser.add_argument("--pdfs", type=int, default=150, help="PDFs per run (default: 150)")
    parser.add_argument("--repeat", type=int, default=1, help="sweeps over all worker counts")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--plot-only", action="store_true", help="re-summarize runs.csv and re-plot"
    )
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    runs_path = OUT / "runs.csv"
    csv_path, png_path = OUT / "results.csv", OUT / "throughput.png"

    if not args.plot_only:
        aws = {
            "s3": boto3.Session(profile_name="pdfsearch-watcher").client("s3"),
            "sqs": boto3.Session(profile_name="pdfsearch-keda").client("sqs"),
            "bucket": terraform_output("bucket_name"),
            "queue_url": terraform_output("queue_url"),
        }
        run_id = time.strftime("%Y%m%d%H%M%S")
        try:
            with Prometheus() as prometheus:
                for repeat in range(1, args.repeat + 1):
                    for n in sorted(args.workers):
                        row = run(n, args.pdfs, args.seed, aws, prometheus, f"{run_id}-{repeat}")
                        # Saved after every run, so partial results survive a failure.
                        append_run({"run_id": run_id, "repeat": repeat, **row}, runs_path)
        finally:
            hold_workers(None)
            log("autoscaling restored")

    rows = summarize(runs_path)
    write_csv(rows, csv_path)
    plot(rows, png_path)
    log(f"wrote {csv_path} and {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
