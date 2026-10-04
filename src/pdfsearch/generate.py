"""pdfsearch-generate: create many PDFs with reproducible text, for load tests.

Every document starts with the line "Generated document NNNN", so a search
for e.g. "document 0042" finds exactly that file. The rest is random
English-like text from a fixed vocabulary; the same seed always produces the
same text, so benchmark runs are comparable.
"""

import argparse
import os
import random
import sys
import time
from pathlib import Path

import pymupdf

_WORDS = """
algorithm analysis application architecture array asynchronous availability
backup balance bandwidth batch benchmark binary block bottleneck buffer bucket
cache capacity channel checkpoint client cloud cluster commit compiler compute
concurrency consensus consistency container control coordinator cost counter
data database deadline deadlock delivery dependency deployment design device
disk distributed document durability elastic election encryption endpoint
error event eventual execution failover failure fault file filter function
gateway graph hash heartbeat heap host idempotent index instance interface
isolation job kernel key latency layer leader lease library limit link load
lock log machine memory message metric migration model monitor network node
object operation optimization order packet page parallel partition password
path peer performance pipeline platform policy pool primary priority process
protocol proxy queue quorum rate read record recovery redundancy region
replica replication request resource response retry ring rollback route
runtime scale schedule schema search secondary security segment server
service session shard signal snapshot socket storage stream subscriber system
table task thread throughput timeout token topology trace transaction tree
trigger update upload user value version virtual volume worker workload write
the a of and to in is that for on with as by from at this which be are an it
can each when all more must not only then than other into between under
"""
VOCABULARY = _WORDS.split()

PAGE_RECT = pymupdf.Rect(56, 56, 539, 786)  # A4 with ~2 cm margins
FONT_SIZE = 10


def page_texts(rng: random.Random, pages: int, words_per_page: int) -> list[str]:
    """Random sentences for each page."""
    texts = []
    for _ in range(pages):
        sentences = []
        remaining = words_per_page
        while remaining > 0:
            length = min(remaining, rng.randint(6, 18))
            words = [rng.choice(VOCABULARY) for _ in range(length)]
            sentences.append(" ".join(words).capitalize() + rng.choice([".", ".", ".", ",", ";"]))
            remaining -= length
        texts.append(" ".join(sentences))
    return texts


def write_pdf(path: Path, texts: list[str]) -> None:
    """Write a PDF with one page per text, atomically.

    The file is written under a hidden temporary name first and then renamed:
    a watcher on the same folder never sees a half-written PDF (it ignores
    hidden files), and the rename is atomic within one filesystem.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    with pymupdf.open() as doc:
        for text in texts:
            page = doc.new_page(width=595, height=842)  # A4 in points
            if page.insert_textbox(PAGE_RECT, text, fontsize=FONT_SIZE) < 0:
                raise ValueError("page text does not fit on the page; use fewer words per page")
        doc.save(tmp)
    os.replace(tmp, path)


def generate(
    folder: Path,
    count: int,
    *,
    min_pages: int = 5,
    max_pages: int = 40,
    words_per_page: int = 250,
    seed: int = 0,
) -> list[Path]:
    """Create count PDFs named generated-NNNN.pdf in folder; return their paths."""
    rng = random.Random(seed)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        texts = page_texts(rng, rng.randint(min_pages, max_pages), words_per_page)
        texts[0] = f"Generated document {i:04}\n\n{texts[0]}"
        path = folder / f"generated-{i:04}.pdf"
        write_pdf(path, texts)
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfsearch-generate",
        description="Create PDFs with reproducible random text, e.g. for load tests.",
    )
    parser.add_argument("folder", type=Path, help="where to write the PDFs (created if missing)")
    parser.add_argument("--count", type=int, default=100, help="number of PDFs (default: 100)")
    parser.add_argument("--min-pages", type=int, default=5, help="default: 5")
    parser.add_argument("--max-pages", type=int, default=40, help="default: 40")
    parser.add_argument("--seed", type=int, default=0, help="same seed, same text (default: 0)")
    args = parser.parse_args(argv)
    if not 1 <= args.min_pages <= args.max_pages:
        parser.error("need 1 <= --min-pages <= --max-pages")

    start = time.perf_counter()
    paths = generate(
        args.folder,
        args.count,
        min_pages=args.min_pages,
        max_pages=args.max_pages,
        seed=args.seed,
    )
    elapsed = time.perf_counter() - start
    pages = sum(pymupdf.open(p).page_count for p in paths)
    size = sum(p.stat().st_size for p in paths)
    print(
        f"Wrote {len(paths)} PDFs, {pages} pages, {size / 1e6:.1f} MB "
        f"to {args.folder} in {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
