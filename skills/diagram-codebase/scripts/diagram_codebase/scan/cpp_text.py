"""Text utilities for regex-based analysis of C-family and JavaScript sources.

`strip_comments` blanks out comments (and optionally string contents) while
preserving newlines and offsets, so regex matches keep correct line numbers.
"""

from __future__ import annotations


def strip_comments(src: str, *, keep_strings: bool = True, js: bool = False) -> str:
    out = list(src)
    i, n = 0, len(src)
    quotes = ("'", '"', "`") if js else ("'", '"')
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in quotes:
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n" and c != "`":
                    break
                j += 1
            if not keep_strings:
                for k in range(i + 1, min(j, n)):
                    if out[k] != "\n":
                        out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def match_brace(text: str, open_idx: int) -> int:
    """Index of the brace closing the one at `open_idx` (text must be comment/string-stripped)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1
