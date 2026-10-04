import random

import pymupdf
import pytest

from pdfsearch.extract import extract_pages
from pdfsearch.generate import generate, main, page_texts
from pdfsearch.text import normalize


def extracted(path):
    return [normalize(text) for _, text in extract_pages(path)]


def test_creates_numbered_files_with_page_counts_in_range(tmp_path):
    paths = generate(tmp_path, 5, min_pages=2, max_pages=4)

    assert [p.name for p in paths] == [f"generated-000{i}.pdf" for i in range(5)]
    for path in paths:
        assert 2 <= pymupdf.open(path).page_count <= 4


def test_text_survives_extraction_completely(tmp_path):
    # Nothing is cut off: every generated word can be found by search.
    [path] = generate(tmp_path, 1, min_pages=3, max_pages=3, seed=7)

    rng = random.Random(7)
    expected = page_texts(rng, rng.randint(3, 3), 250)
    expected[0] = f"Generated document 0000\n\n{expected[0]}"

    assert extracted(path) == [normalize(t) for t in expected]


def test_each_document_has_a_unique_marker(tmp_path):
    paths = generate(tmp_path, 3, min_pages=1, max_pages=1)
    for i, path in enumerate(paths):
        assert extracted(path)[0].startswith(f"generated document {i:04} ")


def test_same_seed_same_text(tmp_path):
    a = generate(tmp_path / "a", 2, seed=1)
    b = generate(tmp_path / "b", 2, seed=1)
    c = generate(tmp_path / "c", 2, seed=2)

    assert [extracted(p) for p in a] == [extracted(p) for p in b]
    assert [extracted(p) for p in a] != [extracted(p) for p in c]


def test_leaves_no_temporary_files(tmp_path):
    generate(tmp_path, 3, min_pages=1, max_pages=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "generated-0000.pdf",
        "generated-0001.pdf",
        "generated-0002.pdf",
    ]


def test_command(tmp_path, capsys):
    assert main([str(tmp_path / "out"), "--count", "2", "--max-pages", "6"]) == 0
    assert "Wrote 2 PDFs" in capsys.readouterr().out
    assert len(list((tmp_path / "out").iterdir())) == 2


def test_command_rejects_invalid_page_range(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--min-pages", "5", "--max-pages", "2"])


def test_prefix_tells_batches_apart(tmp_path):
    main([str(tmp_path), "--count", "2", "--max-pages", "5", "--prefix", "batch2-"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["batch2-0000.pdf", "batch2-0001.pdf"]
