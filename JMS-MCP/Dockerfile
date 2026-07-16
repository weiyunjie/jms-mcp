# Pinned to 3.11 (cached locally); project requires-python is >=3.11.
# Third-party registry mirrors were unreachable for 3.12-slim at build time.
FROM python:3.11

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY jumpserver_mcp_server ./jumpserver_mcp_server

RUN pip install --no-cache-dir ".[encoding]"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 jmsmcp \
    && mkdir -p /data \
    && chown -R jmsmcp:jmsmcp /app /data
USER jmsmcp

# Audit DB and batch spill files live under /data (mount a volume to persist).
ENV audit_db_path=/data/jms_mcp_audit.sqlite3 \
    batch_spill_dir=/data/jms_mcp_batch_results \
    server_port=8000

EXPOSE 8000

# Liveness: the HTTP transport mount returns 401 (not 5xx) when reachable but
# unauthorized, so a connection-refused is the real failure signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket,os; s=socket.create_connection(('127.0.0.1', int(os.environ.get('server_port','8000'))), 3); s.close()" || exit 1

CMD ["python", "-m", "jumpserver_mcp_server"]
