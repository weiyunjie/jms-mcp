"""Data handling: encoding normalization, binary detection, truncation.

Implements design.md Decision 13 / the ``data-handling`` spec, adapted to the
ops-job output envelope. Operates on the *reassembled, base64-decoded* bytes
produced by ``command_execution`` (not a raw stream), per spike 0.1.

Three-stage encoding pipeline:
1. detect  — sniff likely encoding (UTF-8 first, then chardet if available,
   then common fallbacks),
2. fallback — if detection is uncertain, decode latin-1 (never raises) so we
   always emit *something* valid,
3. normalize — return a valid UTF-8 ``str``.

Binary detection flags streams that should not be treated as text (NUL bytes,
known magic numbers, or a high ratio of undecodable bytes) so the caller hands
them back as base64 rather than mojibake.
"""

from __future__ import annotations

from logging import getLogger

logger = getLogger(__name__)

# Known binary magic numbers (prefixes) for the stream types the spec calls out:
# tar, gzip/zip, openssl, compressed mysqldump, etc.
_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x1f\x8b",            # gzip
    b"PK\x03\x04",         # zip
    b"PK\x05\x06",         # empty zip
    b"BZh",                  # bzip2
    b"\xfd7zXZ\x00",     # xz
    b"\x37\x7a\xbc\xaf\x27\x1c",  # 7z
    b"Salted__",           # openssl enc
    b"\x89PNG\r\n\x1a\n", # png
    b"\x7fELF",            # ELF binary
    b"ustar",                # tar (at offset 257, but cheap to also check head)
)

# Sample size for binary heuristics on large payloads.
_SNIFF_BYTES = 8192


def _has_magic(data: bytes) -> bool:
    head = data[:512]
    for magic in _MAGIC_PREFIXES:
        if head.startswith(magic):
            return True
    # tar stores "ustar" at offset 257.
    if len(data) >= 263 and data[257:262] == b"ustar":
        return True
    return False


def looks_binary(data: bytes) -> bool:
    """Heuristically decide whether ``data`` is a binary stream.

    True if: it has a known binary magic prefix, contains NUL bytes, or a
    significant fraction of the sniffed window is non-text after a UTF-8 attempt.
    """
    if not data:
        return False
    if _has_magic(data):
        return True
    sample = data[:_SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    # If it decodes cleanly as UTF-8, it's text.
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    # Count bytes outside the typical printable/whitespace range.
    text_bytes = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x0C, 0x0B}
    nontext = sum(1 for b in sample if b not in text_bytes and b < 0x80)
    # High ratio of control (non-UTF8-extending) bytes => binary.
    return (nontext / len(sample)) > 0.30


def _try_chardet(data: bytes) -> str | None:
    """Return a detected encoding name via chardet/charset_normalizer if present."""
    for mod_name in ("charset_normalizer", "chardet"):
        try:
            mod = __import__(mod_name)
        except ImportError:
            continue
        try:
            if mod_name == "charset_normalizer":
                match = mod.from_bytes(data).best()
                if match is not None:
                    return match.encoding
            else:
                guess = mod.detect(data)
                enc = guess.get("encoding")
                conf = guess.get("confidence") or 0
                if enc and conf >= 0.6:
                    return enc
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s detection failed: %r", mod_name, exc)
    return None


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode ``data`` to UTF-8 text. Returns ``(text, encoding_used)``.

    Stage 1: try strict UTF-8 (the common case; emit unchanged).
    Stage 2: try detection (charset_normalizer/chardet) then common Asian/Western
             fallbacks (GBK, Big5, Latin-1).
    Stage 3: latin-1 always succeeds — guarantees valid UTF-8 output, never raises.
    """
    if not data:
        return "", "utf-8"

    # Stage 1 — UTF-8 fast path.
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # Stage 2 — detection + curated fallbacks.
    detected = _try_chardet(data)
    candidates = [c for c in (detected, "gbk", "big5", "shift_jis", "euc-kr") if c]
    seen: set[str] = set()
    for enc in candidates:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # Stage 3 — latin-1 never fails; normalize to UTF-8 implicitly via str.
    return data.decode("latin-1"), "latin-1"


def annotate_truncation(text: str, *, returned: int, total: int, cap: int) -> str:
    """Wrap ``text`` with before/after truncation banners (spec 7.3)."""
    banner = f"[output truncated: returned {returned} of {total} bytes, cap {cap}]"
    return f"{banner}\n{text}\n{banner}"
