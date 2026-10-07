"""Small string predicates shared by the client and the tools."""

from __future__ import annotations


def is_ascii_digits(text: str) -> bool:
    """``str.isdigit`` also accepts characters such as ``²`` that ``int()`` rejects."""
    return text.isascii() and text.isdigit()
