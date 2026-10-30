"""Untrusted text (tender fields, document text, model output) is normalised before any guard looks at it
and before it is written into a brief: no zero-width/bidi tricks, no full-width evasion, no forged line structure."""
import re
import unicodedata

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"), None)


def normalize(text) -> str:
    return unicodedata.normalize("NFKC", text if isinstance(text, str) else ("" if text is None else str(text))).translate(_INVISIBLE)


def oneline(text) -> str:
    """Collapse ALL whitespace (including newlines) so a field can never start a new markdown line or section."""
    return re.sub(r"\s+", " ", normalize(text)).strip()
