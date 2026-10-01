import re

import pytest

from pdfsearch.text import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Case folding
        ("Hello", "hello"),
        ("MiXeD CaSe 123", "mixed case 123"),
        # Punctuation becomes a word boundary
        ("Hello, Everyone!", "hello everyone"),
        ("don't", "don t"),
        ("end.Next", "end next"),
        ("C++", "c"),
        ("e.g.", "e g"),
        # Whitespace runs collapse, ends are trimmed
        ("  multiple   spaces  ", "multiple spaces"),
        ("tabs\tand\nnewlines\r\n", "tabs and newlines"),
        ("word -- word", "word word"),
        # Non-ASCII characters are discarded
        ("café", "caf"),
        ("naïve", "na ve"),
        ("K", ""),  # Kelvin sign: lowercases to ASCII "k", must still be dropped
        ("ﬁle", "le"),  # "fi" ligature: known limitation, not expanded
        # Digits are kept
        ("Page 12 of 300", "page 12 of 300"),
        # Nothing left
        ("", ""),
        ("   ", ""),
        ("!!!", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


TRICKY_INPUTS = [
    "Hello, Everyone!",
    "  \t\n ",
    "a--b__c..d",
    "Ünïcödé ünd ASCII",
    "x" * 1000 + "!" * 1000 + "Y",
    "Kİß",  # Kelvin sign, dotted capital I, sharp s
]


@pytest.mark.parametrize("raw", TRICKY_INPUTS)
def test_output_format(raw):
    result = normalize(raw)
    assert re.fullmatch(r"([a-z0-9]+( [a-z0-9]+)*)?", result)


@pytest.mark.parametrize("raw", TRICKY_INPUTS)
def test_idempotent(raw):
    once = normalize(raw)
    assert normalize(once) == once
