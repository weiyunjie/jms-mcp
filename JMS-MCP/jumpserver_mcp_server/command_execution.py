"""Command execution over JumpServer ops jobs.

JumpServer's adhoc ops-job ``summary`` is an Ansible play-recap: it carries no
stdout/stderr/exit-code channel (see ``spike-0.1-findings.md``). The only way
to get a command's real output back is to force everything onto the
``summary.failures`` channel by redirecting to stderr and forcing a non-zero
exit. That channel also has a hard ~256 KiB ceiling above which JumpServer
silently drops the *entire* value.

This module implements the chunked-base64 wrapper that works around all of
that:

Dispatch wrapper (one ops job)::

    ( <user-cmd>; echo "__RC__:$?"; ) 2>&1 | base64 | <stage to temp + split>

- ``2>&1`` merges the user command's stdout and stderr.
- The user command runs in a ``( )`` *subshell* so a ``exit N`` inside it ends
  only the subshell — the ``echo "__RC__:$?"`` still runs and captures the
  real exit code as a trailing marker (a ``{ }`` group would let ``exit`` kill
  the whole wrapper and lose the marker).
- ``base64`` makes the payload binary-safe so it survives the JSON string field.
- The base64 text is written to a temp file on the host and ``split`` into
  parts that each stay under the 256 KiB ceiling. The wrapper reports the part
  count, total size, and temp path on stderr (forced ``exit 1``), so the client
  can fetch each part with a follow-up ops job, reassemble, and decode.

Small outputs (a single part) are returned in one round trip; only larger
outputs pay for extra part-fetch jobs.
"""

from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

from .config import settings
from .errors import ConnectionInterruptedError
from .ops_executor import OpsJobExecutor, OpsJobResult, build_ops_job_payload

logger = getLogger(__name__)

RC_MARKER = "__RC__:"
META_MARKER = "__JMSMCP_META__:"
# Marker the dispatch wrapper prints (on stderr) describing the staged output.
# e.g. __JMSMCP_META__:parts=3;size=412345;tmp=/tmp/jmsmcp.XXXX
_META_RE = re.compile(
    r"__JMSMCP_META__:parts=(?P<parts>\d+);size=(?P<size>\d+);tmp=(?P<tmp>\S+)"
)
_FAIL_PREFIX = "shell: "
_FAIL_SUFFIX = ";non-zero return code"


@dataclass
class CommandResult:
    """Structured result of a single command execution."""

    stdout: str = ""
    exit_code: int | None = None
    is_binary: bool = False
    binary_base64: str | None = None
    truncated: bool = False
    encoding_detected: str | None = None
    bytes_total: int | None = None
    bytes_returned: int | None = None
    host: str | None = None
    runas: str | None = None
    command: str | None = None
    status: str = "ok"
    task_id: str | None = None
    job_id: str | None = None
    time_cost: float | None = None
    note: str | None = None
    raw_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "exit_code": self.exit_code,
            "host": self.host,
            "runas": self.runas,
            "command": self.command,
            "truncated": self.truncated,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "time_cost": self.time_cost,
        }
        if self.is_binary:
            out["is_binary"] = True
            out["binary_base64"] = self.binary_base64
            out["bytes_total"] = self.bytes_total
            out["bytes_returned"] = self.bytes_returned
        else:
            out["stdout"] = self.stdout
            if self.encoding_detected:
                out["encoding_detected"] = self.encoding_detected
        if self.note:
            out["note"] = self.note
        return out


def _q(s: str) -> str:
    return shlex.quote(s)


def build_dispatch_wrapper(user_command: str, *, chunk_bytes: int | None = None) -> str:
    """Wrap a user command for capture: merge streams, tag RC, base64, stage.

    The wrapped script:
    1. runs the user command, merging stdout+stderr and appending an RC marker,
    2. base64-encodes the combined stream (binary-safe),
    3. writes it to a temp file and ``split``s it into <256 KiB parts,
    4. prints a metadata line (part count / size / temp dir) to stderr and
       forces ``exit 1`` so the metadata lands in ``summary.failures``.

    Part files are named ``<tmpdir>/part.aa`` etc.; the client fetches them with
    :func:`build_fetch_part_command`.
    """
    chunk = chunk_bytes if chunk_bytes is not None else settings.output_chunk_bytes
    # Run the user command in a SUBSHELL `( ... )` so a literal `exit N` inside
    # it terminates only the subshell — not our wrapper — and the following
    # `echo "__RC__:$?"` still records the real exit code. A `{ ...; }` group
    # would let `exit` kill the whole wrapper and lose the RC marker.
    return (
        "set +e; "
        "__d=$(mktemp -d); "
        "{ ( " + user_command + " ); echo \"" + RC_MARKER + "$?\"; } 2>&1 "
        "| base64 | tr -d '\\n' > \"$__d/full.b64\"; "
        "__sz=$(wc -c < \"$__d/full.b64\"); "
        f"split -b {int(chunk)} \"$__d/full.b64\" \"$__d/part.\"; "
        "__n=$(ls \"$__d/\"part.* 2>/dev/null | wc -l | tr -d ' '); "
        "echo \"" + META_MARKER + "parts=$__n;size=$__sz;tmp=$__d\" 1>&2; "
        "exit 1"
    )


def build_fetch_part_command(tmp_dir: str, part_name: str) -> str:
    """Emit one staged base64 part on the failures channel."""
    path = f"{tmp_dir}/{part_name}"
    return f"cat {_q(path)} 1>&2; exit 1"


def build_cleanup_command(tmp_dir: str) -> str:
    """Remove the staged temp directory once parts are fetched."""
    # Constrain to our mktemp -d location for safety.
    return f"rm -rf {_q(tmp_dir)} 1>&2; exit 1"


def list_part_names(count: int) -> list[str]:
    """Reproduce ``split`` default 2-char suffixes: part.aa, part.ab, ..."""
    names: list[str] = []
    letters = "abcdefghijklmnopqrstuvwxyz"
    for i in range(count):
        hi, lo = divmod(i, 26)
        names.append(f"part.{letters[hi]}{letters[lo]}")
    return names


def extract_failures_body(result: OpsJobResult, host: str | None = None) -> str:
    """Pull the raw text the wrapper pushed onto ``summary.failures``.

    Strips the ``shell: `` prefix and ``;non-zero return code`` suffix that
    JumpServer wraps around the stderr text.
    """
    failures = result.failures
    if host is not None and host in failures:
        raw = failures[host]
    else:
        raw = next(iter(failures.values()), "")
    return raw.removeprefix(_FAIL_PREFIX).removesuffix(_FAIL_SUFFIX)


def parse_dispatch_metadata(body: str) -> dict[str, Any] | None:
    """Parse the ``__JMSMCP_META__`` line from the dispatch wrapper's output."""
    match = _META_RE.search(body)
    if not match:
        return None
    return {
        "parts": int(match.group("parts")),
        "size": int(match.group("size")),
        "tmp": match.group("tmp"),
    }


def _decode_payload(b64_text: str) -> tuple[bytes, int | None]:
    """Base64-decode the reassembled payload and split off the RC marker.

    Returns ``(combined_bytes, exit_code)``. The RC marker is the last line of
    the *decoded* stream (``__RC__:<code>``).
    """
    cleaned = "".join(b64_text.split())
    try:
        raw = base64.b64decode(cleaned, validate=False)
    except Exception as exc:  # noqa: BLE001 - surface as empty decode
        logger.warning("base64 decode failed: %r", exc)
        return b"", None

    marker = RC_MARKER.encode()
    idx = raw.rfind(marker)
    exit_code: int | None = None
    if idx != -1:
        tail = raw[idx + len(marker):]
        # RC marker is followed by digits then a trailing newline.
        digits = tail.split(b"\n", 1)[0].strip()
        try:
            exit_code = int(digits)
        except ValueError:
            exit_code = None
        # Drop the marker (and the newline echo printed before it).
        body = raw[:idx]
        if body.endswith(b"\n"):
            body = body[:-1]
        raw = body
    return raw, exit_code


def finalize_output(combined: bytes, *, max_bytes: int | None = None) -> CommandResult:
    """Apply Section 7 handling (encoding / binary / truncation) to raw bytes."""
    cap = max_bytes if max_bytes is not None else settings.max_output_bytes
    result = CommandResult()
    result.bytes_total = len(combined)

    truncated = False
    if len(combined) > cap:
        combined = combined[:cap]
        truncated = True
    result.truncated = truncated
    result.bytes_returned = len(combined)

    # Lazy import to keep this module importable without the data layer.
    from .data_handling import decode_text, looks_binary

    if looks_binary(combined):
        result.is_binary = True
        result.binary_base64 = base64.b64encode(combined).decode("ascii")
        if truncated:
            result.note = (
                f"binary output truncated to {cap} bytes "
                f"(total {result.bytes_total} bytes)"
            )
        return result

    text, encoding = decode_text(combined)
    if truncated:
        banner = f"[output truncated to {cap} bytes of {result.bytes_total}]"
        text = f"{banner}\n{text}\n{banner}"
    result.stdout = text
    result.encoding_detected = encoding
    return result


class CommandRunner:
    """Run a single shell command on one asset and return structured output."""

    def __init__(self, executor: OpsJobExecutor) -> None:
        self._executor = executor

    async def run(
        self,
        *,
        command: str,
        asset_id: str,
        runas: str,
        host_name: str | None = None,
        timeout: int | None = None,
        chdir: str | None = None,
        max_bytes: int | None = None,
    ) -> CommandResult:
        """Execute ``command`` on ``asset_id`` as ``runas`` and parse output."""
        wrapper = build_dispatch_wrapper(command)
        payload = build_ops_job_payload(
            name="mcp-exec",
            args=wrapper,
            asset_ids=[asset_id],
            runas=runas,
            timeout=timeout,
            chdir=chdir,
            comment="mcp command execution",
        )
        dispatch = await self._executor.run(payload)

        # A dark host = target unreachable; raise the right category.
        target = host_name or (dispatch.ok_hosts[0] if dispatch.ok_hosts else None)
        if dispatch.dark:
            OpsJobExecutor.classify_host_failure(dispatch, next(iter(dispatch.dark)))

        body = extract_failures_body(dispatch)
        meta = parse_dispatch_metadata(body)
        if meta is None:
            # No metadata marker — either truly empty output or a permission/other
            # failure surfaced as plain text. Surface what we have.
            result = CommandResult(stdout=body.strip())
            result.status = "ok"
            result.host = target
            result.runas = runas
            result.command = command
            result.task_id = dispatch.task_id
            result.job_id = dispatch.job_id
            result.time_cost = dispatch.time_cost
            result.raw_summary = dispatch.summary
            return result

        b64_text = await self._fetch_parts(
            meta, asset_id=asset_id, runas=runas, timeout=timeout
        )
        combined, exit_code = _decode_payload(b64_text)
        result = finalize_output(combined, max_bytes=max_bytes)
        result.exit_code = exit_code
        result.host = target
        result.runas = runas
        result.command = command
        result.task_id = dispatch.task_id
        result.job_id = dispatch.job_id
        result.time_cost = dispatch.time_cost
        result.raw_summary = dispatch.summary

        # Best-effort cleanup of the staged temp dir.
        await self._cleanup(meta["tmp"], asset_id=asset_id, runas=runas)
        return result

    async def _fetch_parts(
        self,
        meta: dict[str, Any],
        *,
        asset_id: str,
        runas: str,
        timeout: int | None,
    ) -> str:
        """Fetch and concatenate the staged base64 parts."""
        parts = meta["parts"]
        tmp = meta["tmp"]
        if parts <= 0:
            return ""
        pieces: list[str] = []
        for part_name in list_part_names(parts):
            cmd = build_fetch_part_command(tmp, part_name)
            payload = build_ops_job_payload(
                name="mcp-exec-part",
                args=cmd,
                asset_ids=[asset_id],
                runas=runas,
                timeout=timeout,
                comment="mcp command output part fetch",
            )
            part_result = await self._executor.run(payload)
            pieces.append(extract_failures_body(part_result))
        return "".join(pieces)

    async def _cleanup(self, tmp_dir: str, *, asset_id: str, runas: str) -> None:
        if not tmp_dir or not tmp_dir.startswith("/tmp/"):
            return
        try:
            payload = build_ops_job_payload(
                name="mcp-exec-cleanup",
                args=build_cleanup_command(tmp_dir),
                asset_ids=[asset_id],
                runas=runas,
                comment="mcp command output cleanup",
            )
            await self._executor.run(payload)
        except (ConnectionInterruptedError, Exception) as exc:  # noqa: BLE001
            logger.debug("cleanup of %s failed (non-fatal): %r", tmp_dir, exc)
