import pytest

from pdfsearch.index import index_page
from pdfsearch.search import PageMatch, QueryTooShortError, SearchStats, search


def test_example_from_spec(redis):
    index_page(redis, "first_lecture.pdf", 12, "Hello everyone")
    assert search(redis, "every") == [PageMatch("first_lecture.pdf", 12)]


@pytest.mark.parametrize("query", ["EVERY", "Every!", "  every  ", "lo ev", "Hello, everyone."])
def test_query_is_normalized_like_the_page(redis, query):
    index_page(redis, "a.pdf", 1, "Hello everyone")
    assert search(redis, query) == [PageMatch("a.pdf", 1)]


def test_only_matching_pages_are_returned(redis):
    index_page(redis, "a.pdf", 1, "distributed systems")
    index_page(redis, "a.pdf", 2, "operating systems")
    index_page(redis, "b.pdf", 1, "distributed databases")
    assert search(redis, "distributed") == [PageMatch("a.pdf", 1), PageMatch("b.pdf", 1)]


def test_false_positive_is_filtered_out(redis):
    # The page contains every trigram of "abcde" (abc, bcd, cde),
    # but not "abcde" itself. Verification must reject it.
    index_page(redis, "a.pdf", 1, "abcd xcde")
    assert search(redis, "abcde") == []


def test_unknown_trigram_returns_nothing(redis):
    index_page(redis, "a.pdf", 1, "hello")
    assert search(redis, "xyz") == []


def test_empty_index_returns_nothing(redis):
    assert search(redis, "anything") == []


def test_results_sorted_by_file_then_numeric_page(redis):
    for file, page in [("b.pdf", 1), ("a.pdf", 10), ("a.pdf", 2), ("a.pdf", 1)]:
        index_page(redis, file, page, "common text")
    assert search(redis, "common") == [
        PageMatch("a.pdf", 1),
        PageMatch("a.pdf", 2),
        PageMatch("a.pdf", 10),
        PageMatch("b.pdf", 1),
    ]


def test_filename_containing_hash(redis):
    index_page(redis, "notes#2.pdf", 7, "trigram index")
    assert search(redis, "index") == [PageMatch("notes#2.pdf", 7)]


def test_three_characters_is_enough(redis):
    index_page(redis, "a.pdf", 1, "abc")
    assert search(redis, "abc") == [PageMatch("a.pdf", 1)]


@pytest.mark.parametrize("query", ["", "ab", "  ab  ", "C++", "!!!", "éé ab"])
def test_too_short_after_normalization(redis, query):
    with pytest.raises(QueryTooShortError):
        search(redis, query)


def test_stats_count_false_positives(redis):
    # Same setup as test_false_positive_is_filtered_out: 1 candidate, 0 matches.
    index_page(redis, "a.pdf", 1, "abcd xcde")
    stats = SearchStats()

    assert search(redis, "abcde", stats=stats) == []

    assert (stats.candidates, stats.matches) == (1, 0)
    assert stats.intersect_seconds > 0
    assert stats.verify_seconds > 0


def test_stats_without_candidates(redis):
    stats = SearchStats()
    search(redis, "nothing", stats=stats)
    assert (stats.candidates, stats.matches, stats.verify_seconds) == (0, 0, 0)
