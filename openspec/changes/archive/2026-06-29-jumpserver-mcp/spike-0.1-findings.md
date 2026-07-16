# Spike 0.1 Findings — JumpServer Ops Job `summary` response format

**Date:** 2026-06-29
**Target asset:** `203.0.113.10` (asset id `00000000-0000-0000-0000-000000000000`, runas `ec2-user`)
**Method:** real read-only ad-hoc ops jobs via `POST /ops/jobs/` + poll `GET /ops/job-execution/task-detail/{task_id}/`
**Scripts:** `JMS-MCP/spike_ops_summary.py`, `JMS-MCP/spike_threshold.py`

## TL;DR — the design's output assumptions are wrong

JumpServer's ad-hoc Ops Job does **not** expose a stdout/stderr/exit-code channel. The
`task-detail` `summary` is an **Ansible play-recap**: host-status buckets only. This
contradicts design.md Decision 13 and the `data-handling` spec, which assumed a raw
stream and a 100MB cap. See "Design implications" below.

## What `task-detail` actually returns

`GET /ops/job-execution/task-detail/{task_id}/` →

```json
{
  "status": {"value": "success", "label": "Success"},
  "is_finished": true,
  "is_success": true,
  "time_cost": 2.366915,
  "job_id": "c971f8fa-...",
  "summary": {
    "ok": ["demo-linux-host"],   // list of hostnames only — NO output
    "failures": {},            // {hostname: "shell: <text>;non-zero return code"}
    "dark": {},                // unreachable / connection failures
    "skipped": []
  }
}
```

- **No stdout/stderr/exit-code fields exist.** `summary` is the Ansible recap.
- On a **successful** command (exit 0), the host name lands in `ok` as a bare string.
  **stdout is discarded** — there is no field that carries it.
- On a **non-zero exit**, the host lands in `failures` with value
  `"shell: <stderr-text>;non-zero return code"`. Confirmed: **stdout is still dropped**;
  only stderr text is embedded. (Test: `echo to-stdout; echo to-stderr 1>&2; exit 2`
  → `failures` held only `to-stderr`.)
- `dark` is for unreachable hosts (connection-level failure), `skipped` for skipped tasks.
- `/ops/job-executions/?job_id=...` and `/ops/job-executions/{id}/` return the same
  `summary` plus metadata (`material`, `date_start/finished`, `creator`); still no output channel.
- No working per-execution log endpoint found (`.../log/` variants → 404).

## Who controls encoding / is output pre-truncated

- Output is embedded as a **JSON string** inside `summary.failures[host]`. Encoding is
  whatever Ansible captured, surfaced as a JSON-escaped string — the daemon does not get
  a raw byte stream to re-decode. The three-stage encoding pipeline (task 7.1) has to
  operate on this already-decoded string, not on raw bytes.
- **Output IS pre-truncated — hard.** See ceiling below.

## The only way to retrieve command output

The existing `find_large_log_paths` tool already discovered the workaround, now confirmed
as the *only* option: **redirect stdout→stderr and force a non-zero exit** so the text
lands in `summary.failures`:

```
<command> 1>&2; exit 1
```

Then strip the `"shell: "` prefix and `";non-zero return code"` suffix
(`parse_large_log_output` already does this). Without this trick, a normal exit-0 command
returns **no output at all**.

## Output-size ceiling (critical)

The `failures` channel has a hard ceiling around **256 KiB**, and above it JumpServer
**drops the entire value to empty** rather than truncating:

| stdout size sent | body chars returned | result |
|---|---|---|
| ~128 KB (20k lines) | 108,893 | OK |
| ~262 KB (40k lines) | 228,893 | OK |
| ~288 KB (44k lines) | 252,893 | OK |
| ~301 KB (46k lines) | **0** | **dropped to empty** |
| ~328 KB (50k lines) | **0** | **dropped to empty** |

Cutoff ≈ **262,144 bytes (256 KiB)**. This is drop-to-empty, not graceful truncation —
exceeding it loses *all* output silently.

## Design implications (must resolve before Section 5/7)

1. **No success-path output.** Every command we want output from must use the
   `1>&2; exit 1` trick. That means we cannot distinguish a real command failure from the
   "force-fail to capture output" convention using exit code alone — we must own the exit
   convention and parse accordingly. The real exit code has to be captured *inside* the
   command (e.g. append `; echo "__rc=$?" 1>&2`) and parsed out of the text.
2. **256 KiB cap, not 100 MB.** design.md Decision 13 / `data-handling` spec assume a
   configurable 100 MB cap with truncation annotations. Reality: a ~256 KiB hard ceiling
   that drops to empty. We must truncate **on the target host** (e.g. pipe through
   `head -c 200000`) *before* it hits this limit, and annotate truncation ourselves —
   the JumpServer layer will not do it for us.
3. **Binary output** cannot ride this channel safely (it is a JSON string field). Binary
   must be base64-encoded on the host before capture, then size-capped like text.
4. **stderr vs stdout are merged** by the redirect trick. To separate them (command-execution
   spec requires both) we must capture them to separate markers in the command wrapper and
   split them back out in the parser.

## RESOLVED: output-capture strategy (chunked base64 wrapper)

Chosen approach for command execution (overrides design.md Decision 13's 100 MB inline assumption):

**Command wrapper** — every user command is wrapped so stdout+stderr merge, the real
exit code is captured, and the payload is binary-safe:

```sh
{ <user-cmd>; echo "__RC__:$?"; } 2>&1 | base64
```

Because base64 of the combined output still rides the ~256 KiB `failures` field, large
output is **split on the host** into ~200 KB parts and fetched via separate ops jobs,
then reassembled and decoded client-side:

- Host writes the base64 stream to a temp file, `split` into ~200KB parts.
- Part count N is reported; client fetches part 1..N via separate ops jobs (each part
  stays under the 256 KiB ceiling).
- Client concatenates parts, base64-decodes, then splits off the trailing `__RC__:<code>`
  marker to recover the real exit code, and applies encoding/binary handling (Section 7).

**Properties gained:** merged stdout+stderr, real exit code, binary-safe, no silent
drop (each part is bounded). The configurable "max output" cap (Section 7.3) now governs
how many parts we are willing to fetch/reassemble before annotating truncation, rather
than a single 256 KiB blob.

**Spec deltas to reconcile during Section 5/7 (noted, not yet edited):**
- `data-handling` 100 MB default cap → reinterpreted as a client-side reassembly cap; the
  per-job transport unit is ~200 KB.
- `command-execution` "separate stdout/stderr" → merged by default (`2>&1`); a future
  variant can use distinct markers if true separation is required.
