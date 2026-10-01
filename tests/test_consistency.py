"""Whole-system tests of the index against generated text.

- Equivalence: search() returns exactly what a brute-force scan returns.
- Idempotency: indexing everything twice leaves Redis unchanged.
- Concurrency: many concurrent writers produce the same index as one writer.
"""

import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from pdfsearch.index import index_page, trigram_key
from pdfsearch.search import MIN_QUERY_LENGTH, PageMatch, QueryTooShortError, search
from pdfsearch.text import normalize, trigrams

SEEDS = [0, 1, 2]

# A small alphabet makes pages share many trigrams, so the trigram filter
# produces plenty of false positives that verification has to reject.
ALPHABET = "abcde"
SEPARATORS = [" ", " ", " ", ", ", ". ", "\n", " - ", "  "]


def random_word(rng: random.Random) -> str:
    word = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 6)))
    if rng.random() < 0.1:
        word = word.upper()
    if rng.random() < 0.05:
        word += "é"
    return word


def random_corpus(rng: random.Random) -> dict[tuple[str, int], str]:
    """Raw text of random pages, keyed by (file, page)."""
    pages = {}
    for f in range(10):
        for page in range(1, rng.randint(1, 15) + 1):
            text = ""
            for _ in range(rng.randint(0, 40)):
                text += random_word(rng) + rng.choice(SEPARATORS)
            pages[(f"doc{f}.pdf", page)] = text
    return pages


def random_queries(rng: random.Random, corpus: dict[tuple[str, int], str]) -> list[str]:
    texts = [text for text in corpus.values() if text]
    queries = []
    # Substrings of real pages, taken from the raw text so they include
    # punctuation and mixed case: mostly matches.
    for _ in range(200):
        text = rng.choice(texts)
        start = rng.randrange(len(text))
        queries.append(text[start : start + rng.randint(1, 15)])
    # Random strings over the same alphabet: their trigrams usually exist in
    # the index, but the whole string often does not. Mostly false positives.
    for _ in range(200):
        queries.append("".join(rng.choice(ALPHABET + " ") for _ in range(rng.randint(3, 10))))
    return queries


def brute_force(corpus: dict[tuple[str, int], str], query: str) -> list[PageMatch]:
    """Reference implementation: scan every page, no index."""
    q = normalize(query)
    return sorted(PageMatch(f, p) for (f, p), text in corpus.items() if q in normalize(text))


def index_corpus(redis, corpus: dict[tuple[str, int], str]) -> None:
    for (file, page), text in corpus.items():
        index_page(redis, file, page, text)


def snapshot(redis) -> dict:
    """Every key in the database with its value, for comparing index states."""
    state = {}
    for key in redis.scan_iter():
        if redis.type(key) == "set":
            state[key] = redis.smembers(key)
        else:
            state[key] = redis.get(key)
    return state


@pytest.mark.parametrize("seed", SEEDS)
def test_search_matches_brute_force(redis, seed):
    rng = random.Random(seed)
    corpus = random_corpus(rng)
    index_corpus(redis, corpus)

    with_results = without_results = false_positives = 0
    for query in random_queries(rng, corpus):
        if len(normalize(query)) < MIN_QUERY_LENGTH:
            with pytest.raises(QueryTooShortError):
                search(redis, query)
            continue

        expected = brute_force(corpus, query)
        assert search(redis, query) == expected, query

        if expected:
            with_results += 1
        else:
            without_results += 1
        candidates = redis.sinter([trigram_key(t) for t in trigrams(normalize(query))])
        if len(candidates) > len(expected):
            false_positives += 1

    # Guard against a vacuous test: all three situations must actually occur.
    assert with_results > 50
    assert without_results > 50
    assert false_positives > 50


@pytest.mark.parametrize("seed", SEEDS)
def test_indexing_twice_changes_nothing(redis, seed):
    corpus = random_corpus(random.Random(seed))
    index_corpus(redis, corpus)
    once = snapshot(redis)

    # Re-index in a different order, as a redelivered queue message would.
    items = list(corpus.items())
    random.Random(seed).shuffle(items)
    index_corpus(redis, dict(items))

    assert snapshot(redis) == once


@pytest.mark.parametrize("seed", SEEDS)
def test_concurrent_writers_match_single_writer(redis, seed):
    rng = random.Random(seed)
    corpus = random_corpus(rng)

    index_corpus(redis, corpus)
    single_writer = snapshot(redis)
    redis.flushdb()

    # Every page twice (simulating redelivery), shuffled, across 8 threads
    # sharing one connection pool, so their commands interleave in Redis.
    jobs = list(corpus.items()) * 2
    rng.shuffle(jobs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(index_page, redis, f, p, text) for (f, p), text in jobs]
    for future in futures:
        future.result()  # re-raise any exception from a worker thread

    assert snapshot(redis) == single_writer
