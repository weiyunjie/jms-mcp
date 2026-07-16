from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    server_port: int = 8099
    api_key: str = ""
    api_base_url: str = ""
    api_token: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    access_key_include_host: bool = False
    access_key_sign_query: bool = False
    base_path: str = "/mcp"
    swagger_url: str = ""
    log_level: str = "INFO"
    debug: bool = False
    jumpserver_url: str = ""

    # --- Session / execution tunables (all overridable via env) ---
    # Idle timeout before a logical session context is discarded.
    session_idle_timeout_seconds: int = 15 * 60
    # Max concurrent in-flight ops jobs; executions beyond this queue.
    max_concurrent_jobs: int = 10
    # Client-side reassembly cap for a single command's output, in bytes.
    # NOTE: JumpServer's per-job failures channel has a hard ~256 KiB ceiling
    # (see spike 0.1 findings); large output is split into parts on the host
    # and reassembled client-side up to this cap.
    max_output_bytes: int = 100 * 1024 * 1024
    # Per-job transport chunk size on the host (must stay < 256 KiB ceiling).
    output_chunk_bytes: int = 200 * 1024
    # Tier-2 human approval timeout before auto-deny.
    approval_timeout_seconds: int = 5 * 60
    # Main command gate: "blacklist" (default-allow) or "whitelist" (default-deny).
    policy_mode: str = "blacklist"
    # Optional admin-managed policy JSON (tier1/tier2/whitelist/mode). When set
    # and present, overrides the built-in defaults. Never editable via MCP tools.
    policy_config_path: str = ""
    # Per-ops-job timeout and poll budget (seconds).
    ops_job_timeout_seconds: int = 120
    ops_poll_max_attempts: int = 120
    ops_poll_interval_seconds: float = 1.0
    # Retries for JumpServer-unreachable (HTTP API) calls only.
    jumpserver_unreachable_retries: int = 3
    # Local SQLite audit DB path.
    audit_db_path: str = "jms_mcp_audit.sqlite3"
    # Batch: aggregate results larger than this are spilled to a gzip file.
    batch_inline_limit_bytes: int = 256 * 1024
    # Directory for spilled compressed batch result files.
    batch_spill_dir: str = "jms_mcp_batch_results"
    # TTL for the in-memory host-lookup cache.
    host_cache_ttl_seconds: float = 60.0


settings = Settings()


def missing_config() -> list[str]:
    """Return human-readable names of required config that is absent.

    Required to talk to JumpServer: a base URL and some form of auth
    (either an API token or an Access Key id+secret pair).
    """
    missing: list[str] = []
    if not settings.jumpserver_url and not settings.api_base_url:
        missing.append("JUMPSERVER_URL (or API_BASE_URL)")
    has_token = bool(settings.api_token)
    has_access_key = bool(settings.access_key_id and settings.access_key_secret)
    if not has_token and not has_access_key:
        missing.append("API_TOKEN or (ACCESS_KEY_ID + ACCESS_KEY_SECRET)")
    return missing


def missing_config_hint() -> str | None:
    """Return an actionable hint string if config is missing, else None."""
    missing = missing_config()
    if not missing:
        return None
    return (
        "JumpServer MCP is not configured. Set the following environment "
        "variable(s) (e.g. in JMS-MCP/.env): " + ", ".join(missing) + ". "
        "JUMPSERVER_URL is the bastion base URL (e.g. http://host); auth is "
        "either an API_TOKEN or an Access Key pair (ACCESS_KEY_ID + "
        "ACCESS_KEY_SECRET)."
    )
