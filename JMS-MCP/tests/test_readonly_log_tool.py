import pytest

from jumpserver_mcp_server.readonly_tools import (
    build_large_log_command,
    parse_large_log_output,
    validate_large_log_args,
)


def test_build_large_log_command_is_read_only_and_uses_sudo():
    args = validate_large_log_args({"path": "/", "limit": 20, "min_size_mb": 100})

    command = build_large_log_command(args)

    assert "sudo find /" in command
    assert "-size +100M" in command
    assert "-path /proc -prune" in command
    assert "-path /sys -prune" in command
    assert "-path /dev -prune" in command
    assert "-path /run -prune" in command
    assert "-iname '*.log'" in command
    assert "-iname '*.out'" in command
    assert "head -n 20" in command
    assert "1>&2; exit 1" in command
    assert "rm " not in command
    assert "systemctl" not in command


@pytest.mark.parametrize(
    "path",
    [
        "/var/log; rm -rf /",
        "/var/log && reboot",
        "../var/log",
        "var/log",
    ],
)
def test_validate_large_log_args_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        validate_large_log_args({"path": path})


def test_validate_large_log_args_clamps_limit():
    args = validate_large_log_args({"path": "/var/log", "limit": 1000})

    assert args["limit"] == 100


def test_parse_large_log_output_from_summary_failures():
    rows = parse_large_log_output(
        {
            "failures": {
                "demo-linux-host": (
                    "shell: 106\t/var/log/journal/a.journal\n"
                    "93\t/var/log/csv_import_monitor.log;non-zero return code"
                )
            }
        }
    )

    assert rows == [
        {"size_mb": 106, "path": "/var/log/journal/a.journal"},
        {"size_mb": 93, "path": "/var/log/csv_import_monitor.log"},
    ]
