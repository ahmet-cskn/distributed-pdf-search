"""Text normalization shared by indexing and querying.

Both page text and queries must go through exactly the same normalization,
otherwise a query could fail to match text it should match.
"""

import re

TRIGRAM_LENGTH = 3

_NON_ALPHANUMERIC_RUN = re.compile(r"[^A-Za-z0-9]+")


def normalize(text: str) -> str:
    """Reduce text to lowercase ASCII words separated by single spaces.

    Every run of characters outside [A-Za-z0-9] (punctuation, whitespace,
    non-ASCII) becomes one space; the result is lowercased and trimmed.

    >>> normalize("Hello, Everyone!")
    'hello everyone'
    """
    # Replace before lowercasing: str.lower() maps some non-ASCII characters
    # to ASCII (e.g. the Kelvin sign "K" becomes "k"), which would let them
    # slip through. Replacing first guarantees only ASCII letters are lowercased.
    return _NON_ALPHANUMERIC_RUN.sub(" ", text).lower().strip()


def trigrams(text: str) -> set[str]:
    """Return the set of distinct 3-character substrings of text.

    Expects already-normalized text. Spaces are part of trigrams, so matches
    spanning word boundaries can be found. Text shorter than 3 characters
    has no trigrams.

    >>> sorted(trigrams("ab cd"))
    [' cd', 'ab ', 'b c']
    """
    return {text[i : i + TRIGRAM_LENGTH] for i in range(len(text) - TRIGRAM_LENGTH + 1)}
