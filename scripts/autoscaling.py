"""Chart of one autoscaling run: queue length, workers and throughput over time.

Run it after dropping a batch of PDFs into the inbox (README, "Run on
Kubernetes"), once the workers are back at zero. It looks for the most recent
period of activity (jobs in the queue or workers running) in Prometheus and
plots it, from a minute before it starts to a minute after it ends.

Usage (cluster deployed):
    uv run python scripts/autoscaling.py
    uv run python scripts/autoscaling.py --plot-only

Writes docs/benchmark/autoscaling.csv and autoscaling.png. Prometheus keeps
data for 2 days, so the CSV is what allows re-plotting later.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

from benchmark import GRID, OUT, SERIES, STYLE, SURFACE, TEXT, Prometheus, log

QUERIES = {
    # What KEDA scales on: waiting plus in-flight messages.
    "queue": 'max(keda_scaler_metrics_value{scaledObject="worker"})',
    # Every running worker reports one busy series.
    "workers": "count(pdfsearch_worker_busy)",
    "pages_per_s": "sum(rate(pdfsearch_worker_pages_indexed_total[30s]))",
}
STEP = 5  # seconds: the workers' scrape interval
PADDING = 60  # seconds shown before and after the activity
IDLE_GAP = 180  # a pause this long separates two runs


def series(prometheus: Prometheus, promql: str, start: int, end: int, step: int) -> dict:
    """Values by timestamp; timestamps without a value (e.g. no workers) are missing."""
    result = prometheus.query_range(promql, start, end, step)
    return {int(t): float(v) for t, v in result[0]["values"]} if result else {}


def find_run(prometheus: Prometheus, minutes: int) -> tuple[int, int]:
    """Start and end of the most recent activity within the last `minutes`."""
    end = int(time.time())
    start = end - minutes * 60
    step = max(STEP, (end - start) // 1000)
    queue = series(prometheus, QUERIES["queue"], start, end, step)
    workers = series(prometheus, QUERIES["workers"], start, end, step)
    active = sorted(t for t in queue.keys() | workers.keys() if queue.get(t) or workers.get(t))
    if not active:
        raise SystemExit(f"no activity in the last {minutes} minutes")
    run_start = active[-1]
    for t in reversed(active):
        if run_start - t > IDLE_GAP:
            break
        run_start = t
    return run_start, active[-1]


def collect(prometheus: Prometheus, run_start: int, run_end: int) -> list[dict]:
    start, end = run_start - PADDING, run_end + PADDING
    data = {name: series(prometheus, q, start, end, STEP) for name, q in QUERIES.items()}
    return [
        {
            "seconds": t - run_start,
            **{name: round(values.get(t, 0.0), 1) for name, values in data.items()},
        }
        for t in range(start, end + 1, STEP)
    ]


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["seconds", *QUERIES])
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open() as f:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(f)]


def plot(rows: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    plt.rcParams.update(STYLE)
    minutes = [r["seconds"] / 60 for r in rows]
    panels = [
        ("queue", "Jobs in the queue (waiting and in progress)"),
        ("workers", "Worker pods"),
        ("pages_per_s", "Pages indexed per second"),
    ]
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True, facecolor=SURFACE)
    for ax, (field, title) in zip(axes, panels, strict=True):
        values = [r[field] for r in rows]
        ax.set_facecolor(SURFACE)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        ax.set_title(title, loc="left", fontweight="bold")
        # Worker counts change in whole steps; the other two are continuous.
        step = "post" if field == "workers" else None
        ax.fill_between(minutes, values, step=step, color=SERIES[0], alpha=0.15, linewidth=0)
        if step:
            ax.step(minutes, values, where=step, color=SERIES[0], linewidth=2)
        else:
            ax.plot(minutes, values, color=SERIES[0], linewidth=2)

        peak = max(values)
        at = minutes[values.index(peak)]
        ax.annotate(
            f"peak {peak:.0f}",
            (at, peak),
            textcoords="offset points",
            xytext=(6, 4),
            color=TEXT,
            fontsize=9,
        )
        ax.set_ylim(0, peak * 1.25 or 1)
    axes[-1].set_xlabel("Minutes since the first job was queued")
    axes[-1].set_xlim(minutes[0], minutes[-1])

    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--minutes", type=int, default=30, help="how far back to look (default: 30)"
    )
    parser.add_argument("--plot-only", action="store_true", help="re-plot autoscaling.csv")
    args = parser.parse_args()

    csv_path, png_path = OUT / "autoscaling.csv", OUT / "autoscaling.png"
    if not args.plot_only:
        with Prometheus() as prometheus:
            run_start, run_end = find_run(prometheus, args.minutes)
            log(
                f"activity from {time.strftime('%H:%M:%S', time.localtime(run_start))}"
                f" to {time.strftime('%H:%M:%S', time.localtime(run_end))}"
            )
            write_csv(collect(prometheus, run_start, run_end), csv_path)

    plot(read_csv(csv_path), png_path)
    log(f"wrote {csv_path} and {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
