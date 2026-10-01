"""Writing pages into the Redis trigram index.

Key layout (see docs/DESIGN.md §4.3):
    page:<page id>  String  normalized text of the page
    tri:<trigram>   Set     ids of pages containing the trigram

A page id is "<file>#<page>", with 1-based page numbers.
"""

from redis import Redis

from pdfsearch.text import normalize, trigrams


def page_id(file: str, page: int) -> str:
    """Build the id of a page.

    >>> page_id("lecture1.pdf", 12)
    'lecture1.pdf#12'
    """
    return f"{file}#{page}"


def parse_page_id(pid: str) -> tuple[str, int]:
    """Split a page id into (file, page).

    Splits on the last "#", because filenames may contain "#" themselves.

    >>> parse_page_id("notes#2.pdf#7")
    ('notes#2.pdf', 7)
    """
    file, _, page = pid.rpartition("#")
    return file, int(page)


def page_key(pid: str) -> str:
    return f"page:{pid}"


def trigram_key(trigram: str) -> str:
    return f"tri:{trigram}"


def index_page(redis: Redis, file: str, page: int, text: str) -> None:
    """Normalize a page's text and add it to the index.

    Safe to call concurrently from many workers and safe to repeat:
    SET with the same value and SADD of an existing member change nothing.
    """
    pid = page_id(file, page)
    normalized = normalize(text)

    # One round trip for all commands. Not a transaction: other clients'
    # commands may interleave, which is fine because every command is atomic.
    pipe = redis.pipeline(transaction=False)
    # Text first, so a page never becomes a search candidate without its text.
    pipe.set(page_key(pid), normalized)
    for trigram in trigrams(normalized):
        pipe.sadd(trigram_key(trigram), pid)
    pipe.execute()
