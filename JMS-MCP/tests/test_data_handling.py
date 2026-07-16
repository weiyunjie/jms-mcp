"""Tests for data handling: encoding, binary detection, truncation (task 9.5)."""

from jumpserver_mcp_server.command_execution import (
    _decode_payload,
    finalize_output,
    list_part_names,
    parse_dispatch_metadata,
    RC_MARKER,
)
from jumpserver_mcp_server.data_handling import decode_text, looks_binary


# --- Encoding pipeline (7.1) -------------------------------------------------

def test_utf8_passthrough():
    text, enc = decode_text("héllo wörld".encode("utf-8"))
    assert text == "héllo wörld"
    assert enc == "utf-8"


def test_gbk_decoded():
    # A short string is ambiguous to statistical detectors (GBK vs Big5), so
    # use a longer, realistic line of Chinese text for reliable detection.
    original = "服务器磁盘空间不足，请尽快清理日志文件并重启相关服务。系统监控告警已触发。"
    raw = original.encode("gbk")
    text, enc = decode_text(raw)
    assert text == original
    assert enc.lower() in {"gbk", "gb2312", "gb18030", "gb18030-2000"}


def test_latin1_fallback_never_raises():
    # Bytes that are not valid UTF-8 and not cleanly any Asian encoding.
    raw = b"\xff\xfe\x80 some latin-ish \xe9"
    text, enc = decode_text(raw)
    assert isinstance(text, str)  # never raised
    assert enc  # some encoding was chosen


def test_empty_bytes():
    assert decode_text(b"") == ("", "utf-8")


# --- Binary detection (7.2) --------------------------------------------------

def test_detect_gzip_magic():
    assert looks_binary(b"\x1f\x8b\x08\x00rest-of-gzip") is True


def test_detect_zip_magic():
    assert looks_binary(b"PK\x03\x04rest") is True


def test_detect_elf_magic():
    assert looks_binary(b"\x7fELF\x02\x01\x01") is True


def test_detect_nul_bytes():
    assert looks_binary(b"text\x00with-nul") is True


def test_plain_text_not_binary():
    assert looks_binary(b"just normal log output\nwith lines\n") is False


def test_empty_not_binary():
    assert looks_binary(b"") is False


# --- Truncation (7.3) --------------------------------------------------------

def test_truncation_annotates_text():
    big = b"A" * 1000
    res = finalize_output(big, max_bytes=100)
    assert res.truncated is True
    assert res.bytes_total == 1000
    assert res.bytes_returned == 100
    assert "truncated" in res.stdout
    # Banner appears before and after.
    assert res.stdout.count("truncated") >= 2


def test_no_truncation_when_under_cap():
    res = finalize_output(b"small", max_bytes=100)
    assert res.truncated is False
    assert res.stdout == "small"


def test_binary_truncation_returns_base64():
    raw = b"\x00\x01\x02\x03" * 1000  # 4000 bytes, binary
    res = finalize_output(raw, max_bytes=100)
    assert res.is_binary is True
    assert res.truncated is True
    assert res.binary_base64
    assert res.bytes_returned == 100


# --- Output parsing helpers --------------------------------------------------

def test_decode_payload_recovers_exit_code():
    import base64

    combined = b"hello output\n" + (RC_MARKER + "7\n").encode()
    b64 = base64.b64encode(combined).decode()
    body, rc = _decode_payload(b64)
    assert rc == 7
    assert body == b"hello output"


def test_decode_payload_no_marker():
    import base64

    b64 = base64.b64encode(b"no marker here").decode()
    body, rc = _decode_payload(b64)
    assert rc is None
    assert body == b"no marker here"


def test_parse_dispatch_metadata():
    body = "__JMSMCP_META__:parts=3;size=412345;tmp=/tmp/jmsmcp.AbC"
    meta = parse_dispatch_metadata(body)
    assert meta == {"parts": 3, "size": 412345, "tmp": "/tmp/jmsmcp.AbC"}


def test_parse_dispatch_metadata_absent():
    assert parse_dispatch_metadata("some unrelated text") is None


def test_list_part_names_suffixes():
    assert list_part_names(3) == ["part.aa", "part.ab", "part.ac"]
    assert list_part_names(27)[26] == "part.ba"
