"""Tests for command-execution wrapper building and output parsing (task 9.3).

These are pure-logic tests on the chunked-base64 capture machinery — no network.
The wrapper/parse contract is what makes the spike-0.1 workaround correct, so it
is worth locking down: RC marker recovery, base64 round-trip, metadata parsing,
part-name sequence, and the failures-channel prefix/suffix stripping.
"""

import base64

from jumpserver_mcp_server.command_execution import (
    RC_MARKER,
    _decode_payload,
    build_cleanup_command,
    build_dispatch_wrapper,
    build_fetch_part_command,
    extract_failures_body,
    finalize_output,
    list_part_names,
    parse_dispatch_metadata,
)
from jumpserver_mcp_server.ops_executor import OpsJobResult


def test_dispatch_wrapper_uses_subshell_and_markers():
    wrapper = build_dispatch_wrapper("echo hi; exit 7", chunk_bytes=200000)
    # User command runs in a subshell so a literal exit doesn't kill the wrapper.
    assert "( echo hi; exit 7 )" in wrapper
    assert RC_MARKER in wrapper
    assert "base64" in wrapper
    assert "split -b 200000" in wrapper
    assert "__JMSMCP_META__:" in wrapper
    # Forces non-zero exit so output lands on summary.failures.
    assert wrapper.rstrip().endswith("exit 1")


def test_fetch_and_cleanup_commands_are_quoted():
    fetch = build_fetch_part_command("/tmp/jmsmcp.AB", "part.aa")
    assert "/tmp/jmsmcp.AB/part.aa" in fetch
    assert fetch.rstrip().endswith("exit 1")
    cleanup = build_cleanup_command("/tmp/jmsmcp.AB")
    assert "rm -rf" in cleanup
    assert "/tmp/jmsmcp.AB" in cleanup


def test_list_part_names_sequence():
    assert list_part_names(1) == ["part.aa"]
    assert list_part_names(3) == ["part.aa", "part.ab", "part.ac"]
    # Rolls over after 26.
    names = list_part_names(27)
    assert names[25] == "part.az"
    assert names[26] == "part.ba"


def test_extract_failures_body_strips_prefix_suffix():
    result = OpsJobResult(
        task_id="t", job_id="j", status=None, is_finished=True, is_success=False,
        summary={"failures": {"host-1": "shell: PAYLOAD;non-zero return code"}},
    )
    assert extract_failures_body(result) == "PAYLOAD"
    assert extract_failures_body(result, host="host-1") == "PAYLOAD"


def test_parse_dispatch_metadata():
    body = "noise __JMSMCP_META__:parts=3;size=412345;tmp=/tmp/jmsmcp.Xy more"
    meta = parse_dispatch_metadata(body)
    assert meta == {"parts": 3, "size": 412345, "tmp": "/tmp/jmsmcp.Xy"}


def test_parse_dispatch_metadata_missing():
    assert parse_dispatch_metadata("nothing here") is None


def test_decode_payload_recovers_bytes_and_exit_code():
    # Simulate what the host produced: combined output + RC marker, base64'd.
    combined = b"out-line\nerr-line\n"
    raw = combined + f"{RC_MARKER}7\n".encode()
    b64 = base64.b64encode(raw).decode()
    body, exit_code = _decode_payload(b64)
    assert exit_code == 7
    assert body == b"out-line\nerr-line"


def test_decode_payload_no_marker():
    b64 = base64.b64encode(b"just bytes").decode()
    body, exit_code = _decode_payload(b64)
    assert exit_code is None
    assert body == b"just bytes"


def test_finalize_output_text():
    result = finalize_output(b"hello world")
    assert result.is_binary is False
    assert result.stdout == "hello world"
    assert result.truncated is False


def test_finalize_output_binary_base64():
    elf = b"\x7fELF" + b"\x00\x01\x02\x03" * 10
    result = finalize_output(elf)
    assert result.is_binary is True
    assert base64.b64decode(result.binary_base64) == elf


def test_finalize_output_truncates_with_banner():
    big = b"A" * 1000
    result = finalize_output(big, max_bytes=100)
    assert result.truncated is True
    assert result.bytes_total == 1000
    assert result.bytes_returned == 100
    assert "truncated" in result.stdout
