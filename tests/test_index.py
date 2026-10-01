from pdfsearch.index import index_page, page_id, parse_page_id
from pdfsearch.text import trigrams


def test_page_id_round_trip():
    for file, page in [("a.pdf", 1), ("notes#2.pdf", 7), ("#.pdf", 300)]:
        assert parse_page_id(page_id(file, page)) == (file, page)


def trigram_index(redis):
    """Read the whole trigram index back as {trigram: set of page ids}."""
    return {key.removeprefix("tri:"): redis.smembers(key) for key in redis.scan_iter("tri:*")}


def test_stores_normalized_text(redis):
    index_page(redis, "lecture1.pdf", 12, "Hello, Everyone!")
    assert redis.get("page:lecture1.pdf#12") == "hello everyone"


def test_indexes_exactly_the_page_trigrams(redis):
    index_page(redis, "lecture1.pdf", 12, "Hello, Everyone!")
    pid = "lecture1.pdf#12"
    assert trigram_index(redis) == {t: {pid} for t in trigrams("hello everyone")}


def test_pages_sharing_a_trigram(redis):
    index_page(redis, "a.pdf", 1, "abcd")
    index_page(redis, "b.pdf", 3, "bcde")
    index = trigram_index(redis)
    assert index["abc"] == {"a.pdf#1"}
    assert index["bcd"] == {"a.pdf#1", "b.pdf#3"}
    assert index["cde"] == {"b.pdf#3"}


def test_short_page_has_text_but_no_trigrams(redis):
    index_page(redis, "a.pdf", 1, "?! a")
    assert redis.get("page:a.pdf#1") == "a"
    assert trigram_index(redis) == {}


def test_empty_page_is_stored(redis):
    # Pages without a text layer (e.g. scans) still exist in the index.
    index_page(redis, "scan.pdf", 1, "")
    assert redis.get("page:scan.pdf#1") == ""
