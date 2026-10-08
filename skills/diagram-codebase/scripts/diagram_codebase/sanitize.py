"""Label sanitizing: redact secrets/PII and normalize text for Figma.

Everything that leaves the machine as a diagram label goes through
`sanitize_label`. It does two jobs:

1. **Redaction** of things that must never be published: credentials (cloud
   keys, tokens, private keys, `password = ...` pairs, URL userinfo, high-entropy
   strings), e-mail addresses, home-directory paths (reduced to their basename)
   and private IPv4 addresses. Each hit is reported by category so the run can
   say what was removed without repeating it.
2. **Normalization** for Figma's Mermaid importer: no emoji, HTML tags, control
   characters, newlines (real or the literal two-character `\\n`), backticks,
   and collapsed whitespace.

Ordinary identifiers (`create_publisher`, `/api/todos/{}`, `O(n)`) pass through
unchanged.
"""

from __future__ import annotations

import math
import re
from collections import Counter

REDACTED = "[redacted]"

# --- secrets -----------------------------------------------------------------

_PEM_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|$)",
    re.DOTALL,
)

# (category, pattern). Applied in order; every match becomes REDACTED.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "aws_key",
        re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_])"
        ),
    ),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{30,}")),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{10,}")),
    (
        "jwt",
        re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    ),
]

# `password = value`, `API_KEY: "value"`, `db_secret=value` ... keep the key, drop the value.
_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"[A-Za-z0-9_]*)"
    r"(\s*(?::=|=|:)\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s\"',;(){}\[\]]+)"
)
# Values that are clearly not secrets (placeholders, env lookups, nulls).
_ASSIGN_SAFE_VALUE = re.compile(
    r"(?i)^(?:none|null|nil|true|false|undefined|\"\"|''|\[redacted\]|\$\{?[A-Z_][A-Z0-9_]*\}?|os\.environ.*|getenv.*|env\..*|process\.env.*)$"
)

# Numbers (`max_tokens=1000`) and attribute access (`token=self.token`) are code, not secrets.
_CODE_VALUE_RE = re.compile(r"^(?:\d{1,12}|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)$")

_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@\"']+):([^/\s@\"']+)@")

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

_HOME_PATH_RE = re.compile(
    r"(?<![\w.~}])(?:/home/|/Users/|/root(?=/|\b)|[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)"
    r"[^\s\"'`<>|;,()\[\]{}]*"
)

_IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d]|\.\d)")

_HEX_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{24,}(?![A-Za-z0-9+/=_-])")

# --- normalization -------------------------------------------------------------

_HTML_TAGS = [
    "a",
    "abbr",
    "b",
    "big",
    "blockquote",
    "body",
    "br",
    "center",
    "code",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "font",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "header",
    "hr",
    "html",
    "i",
    "iframe",
    "img",
    "input",
    "ins",
    "kbd",
    "label",
    "li",
    "link",
    "mark",
    "meta",
    "nav",
    "object",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "samp",
    "script",
    "section",
    "small",
    "span",
    "strike",
    "strong",
    "style",
    "sub",
    "summary",
    "sup",
    "svg",
    "table",
    "tbody",
    "td",
    "textarea",
    "th",
    "thead",
    "title",
    "tr",
    "tt",
    "u",
    "ul",
    "var",
    "video",
    "audio",
    "button",
    "canvas",
    "embed",
    "math",
]
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(?:" + "|".join(_HTML_TAGS) + r")\b[^<>]*>|<!--.*?-->|<!\[CDATA\[.*?\]\]>",
    re.IGNORECASE | re.DOTALL,
)

# Emoji and pictographs, including joiners/modifiers that only make sense inside emoji.
EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, flags, symbols & pictographs ext.
    "\U0001fc00-\U0001fffd"
    "☀-➿"  # misc symbols, dingbats
    "⌀-⏿"  # misc technical (watch, hourglass, play buttons, ...)
    "⬀-⯿"  # arrows / stars / squares with emoji presentation
    "↔-↙↩↪"
    "‼⁉™ℹⓂ©®"
    "▪▫▶◀◻-◾"
    "〰〽㊗㊙"
    "︀-️"  # variation selectors
    "‍"  # zero-width joiner
    "⃣"  # combining enclosing keycap
    "\U000e0020-\U000e007f"  # tag characters (subdivision flags)
    "]"
)

# C0/C1 controls, line/paragraph separators -> space.
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f  ]")
# Invisible formatting characters (zero-width, bidi overrides, BOM) -> removed.
_INVISIBLE_RE = re.compile("[​‌‎‏‪-‮⁠-⁤⁦-⁩﻿]")
_LITERAL_ESCAPE_RE = re.compile(r"\\[nrt]")
_WS_RE = re.compile(r"\s+")


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _is_private_ipv4(octets: list[int]) -> bool:
    a, b = octets[0], octets[1]
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
        or (a == 169 and b == 254)
    )


def _looks_like_secret_token(tok: str) -> bool:
    """High-entropy, base64-ish token. Requires digits and letters so long identifiers survive."""
    if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
        return False
    threshold = 4.0
    # Path-like or word-separated tokens (`a/b/c_d`, `foo-bar-baz`) need more evidence.
    if tok.count("/") + tok.count("_") + tok.count("-") >= 3:
        threshold = 4.5
    return shannon_entropy(tok) > threshold


def _path_replacement(path: str) -> str:
    """Keep only the basename of a home-directory path; a bare home dir is fully redacted."""
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    if not parts:
        return REDACTED
    if parts[0] == "root":
        rest = parts[1:]
    elif parts[0].endswith(":"):
        rest = parts[3:]  # C: / Users / <name> / ...
    else:
        rest = parts[2:]  # home|Users / <name> / ...
    return rest[-1] if rest else REDACTED


def _redact(text: str, hits: Counter) -> str:
    def sub(category: str, pattern: re.Pattern[str], value: str) -> str:
        def repl(_m: re.Match[str]) -> str:
            hits[category] += 1
            return REDACTED

        return pattern.sub(repl, value)

    text = sub("private_key", _PEM_RE, text)
    for category, pattern in _SECRET_PATTERNS:
        text = sub(category, pattern, text)

    def url_repl(m: re.Match[str]) -> str:
        hits["url_credentials"] += 1
        return f"{m.group(1)}{REDACTED}@"

    text = _URL_CRED_RE.sub(url_repl, text)

    def assign_repl(m: re.Match[str]) -> str:
        value = m.group(3)
        if (
            _ASSIGN_SAFE_VALUE.match(value)
            or REDACTED in value
            or _CODE_VALUE_RE.match(value)
            or m.string[m.end() : m.end() + 1] == "("
        ):
            return m.group(0)
        # `:` is common in prose ("token: refreshed"); only treat it as an assignment
        # when the value is quoted or does not look like a plain word.
        if m.group(2).strip() == ":" and not (
            value[:1] in "\"'" or re.search(r"[0-9]", value) and len(value) >= 8
        ):
            return m.group(0)
        hits["credential_assignment"] += 1
        return f"{m.group(1)}{m.group(2)}{REDACTED}"

    text = _ASSIGN_RE.sub(assign_repl, text)
    text = sub("email", _EMAIL_RE, text)

    def path_repl(m: re.Match[str]) -> str:
        hits["home_path"] += 1
        return _path_replacement(m.group(0))

    text = _HOME_PATH_RE.sub(path_repl, text)

    def ip_repl(m: re.Match[str]) -> str:
        octets = [int(g) for g in m.groups()]
        if any(o > 255 for o in octets) or not _is_private_ipv4(octets):
            return m.group(0)
        hits["private_ip"] += 1
        return REDACTED

    text = _IPV4_RE.sub(ip_repl, text)
    text = sub("high_entropy", _HEX_RE, text)

    def token_repl(m: re.Match[str]) -> str:
        tok = m.group(0)
        if not _looks_like_secret_token(tok):
            return tok
        hits["high_entropy"] += 1
        return REDACTED

    return _TOKEN_RE.sub(token_repl, text)


def normalize_text(text: str) -> str:
    """Figma-safe text: no emoji, HTML, control chars, newlines, backticks; single spaces."""
    text = _HTML_TAG_RE.sub(" ", text)
    text = EMOJI_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _CONTROL_RE.sub(" ", text)
    text = _LITERAL_ESCAPE_RE.sub(" ", text)
    text = text.replace("`", "")
    return _WS_RE.sub(" ", text).strip()


def _sanitize(text: object) -> tuple[str, Counter]:
    hits: Counter = Counter()
    raw = "" if text is None else str(text)
    return normalize_text(_redact(raw, hits)), hits


def sanitize_label(text: object) -> tuple[str, list[str]]:
    """Return (clean text, sorted redaction categories that fired)."""
    clean, hits = _sanitize(text)
    return clean, sorted(hits)


class Redactor:
    """Sanitizes many labels and accumulates redaction counts for a report."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.labels_changed = 0

    def __call__(self, text: object) -> str:
        return self.sanitize(text)

    def sanitize(self, text: object) -> str:
        clean, hits = _sanitize(text)
        if hits:
            self.labels_changed += 1
            self.counts.update(hits)
        return clean

    def report(self) -> dict[str, object]:
        """`{total, labels, by_category}`: counts only, never the redacted values."""
        return {
            "total": sum(self.counts.values()),
            "labels": self.labels_changed,
            "by_category": dict(sorted(self.counts.items())),
        }
