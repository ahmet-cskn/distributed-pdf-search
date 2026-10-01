import re

import pytest

from pdfsearch.text import normalize, trigrams


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Too short for any trigram
        ("", set()),
        ("a", set()),
        ("ab", set()),
        # Exactly one window
        ("abc", {"abc"}),
        # Windows overlap and include spaces
        ("ab cd", {"ab ", "b c", " cd"}),
        ("hello every", {"hel", "ell", "llo", "lo ", "o e", " ev", "eve", "ver", "ery"}),
        # Repeated windows are counted once
        ("aaaa", {"aaa"}),
        ("abab", {"aba", "bab"}),
    ],
)
def test_trigrams(text, expected):
    assert trigrams(text) == expected


SAMPLE_PAGE = normalize("Hello everyone, welcome to the first lecture on distributed systems!")


def test_substring_trigrams_are_subset():
    # The property the index relies on: if the query is a substring of the
    # page, every trigram of the query is also a trigram of the page. So
    # intersecting trigram lookups can never lose a real match.
    page_trigrams = trigrams(SAMPLE_PAGE)
    for start in range(len(SAMPLE_PAGE)):
        for end in range(start + 3, len(SAMPLE_PAGE) + 1):
            query = SAMPLE_PAGE[start:end]
            assert trigrams(query) <= page_trigrams, query


def test_trigram_count_is_bounded():
    text = "the quick brown fox jumps over the lazy dog"
    assert len(trigrams(text)) <= len(text) - 2
