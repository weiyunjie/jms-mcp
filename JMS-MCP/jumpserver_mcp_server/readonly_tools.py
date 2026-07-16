import shlex
from typing import Any


TARGET_ASSET_ID = "00000000-0000-0000-0000-000000000000"
TARGET_ASSET_ADDRESS = "203.0.113.10"
TARGET_RUNAS = "ec2-user"

UNSAFE_PATH_TOKENS = (
    ";",
    "&",
    "|",
    "`",
    "$",
    "\n",
    "\r",
    ">",
    "<",
)


def validate_large_log_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    arguments = arguments or {}
    path = str(arguments.get("path") or "/")
    if not path.startswith("/") or ".." in path or any(token in path for token in UNSAFE_PATH_TOKENS):
        raise ValueError("path must be a safe absolute path")

    limit = int(arguments.get("limit") or 20)
    limit = max(1, min(limit, 100))

    min_size_mb = int(arguments.get("min_size_mb") or 100)
    min_size_mb = max(1, min(min_size_mb, 1024 * 1024))

    return {"path": path, "limit": limit, "min_size_mb": min_size_mb}


def build_large_log_command(arguments: dict[str, Any]) -> str:
    path = shlex.quote(arguments["path"])
    limit = int(arguments["limit"])
    min_size_mb = int(arguments["min_size_mb"])
    prune_paths = (
        "-path /proc -prune -o "
        "-path /sys -prune -o "
        "-path /dev -prune -o "
        "-path /run -prune -o "
        "-path /mnt -prune -o "
        "-path /media -prune -o "
        "-path /tmp -prune -o "
    )
    log_name_filter = (
        "\\( "
        "-iname '*.log' -o "
        "-iname '*.log.*' -o "
        "-iname '*.out' -o "
        "-iname '*.out.*' -o "
        "-iname '*.err' -o "
        "-iname '*.trace' -o "
        "-iname '*log*' "
        "\\)"
    )
    return (
        "set -o pipefail; "
        "{ "
        f"sudo find {path} {prune_paths}"
        f"\\( -type f -size +{min_size_mb}M {log_name_filter} \\) "
        "-exec sudo du -sm {} + 2>/dev/null | "
        "sort -nr | "
        f"head -n {limit}; "
        "} 1>&2; exit 1"
    )


def parse_large_log_output(summary: dict[str, Any]) -> list[dict[str, Any]]:
    failures = summary.get("failures") or {}
    raw_output = next(iter(failures.values()), "")
    raw_output = raw_output.removeprefix("shell: ").removesuffix(";non-zero return code")
    rows = []
    for line in raw_output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        rows.append({"size_mb": int(parts[0]), "path": parts[1]})
    return rows
