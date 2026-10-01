"""Text normalization shared by indexing and querying.

Both page text and queries must go through exactly the same normalization,
otherwise a query could fail to match text it should match.
"""

import re

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
