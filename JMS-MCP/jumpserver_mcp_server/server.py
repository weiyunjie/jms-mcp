"""JumpServer MCP server with Bearer Token and Access Key auth support."""

import base64
import hashlib
import hmac
import asyncio
import json
import typing
from datetime import datetime, timezone
from email.utils import format_datetime
from logging import getLogger
from typing import Any

import httpx
import mcp.types as types
from fastapi import FastAPI, Request, Response
from fastapi_mcp import FastApiMCP
from fastapi_mcp.openapi.convert import convert_openapi_to_mcp_tools
from mcp.server.lowlevel.server import Server

from .config import settings
from .readonly_tools import (
    TARGET_ASSET_ID,
    TARGET_RUNAS,
    build_large_log_command,
    parse_large_log_output,
    validate_large_log_args,
)
from .setup import setup_logging
from .tools import HANDWRITTEN_TOOLS, HANDWRITTEN_TOOL_NAMES, ToolContext

setup_logging(settings.log_level, debug=settings.debug)
logger = getLogger(__name__)

HTTP_OK = 200


class OpenAPISchemaFetchError(Exception):
    pass


FIND_LARGE_LOG_TOOL = types.Tool(
    name="find_large_log_paths",
    description=(
        "Find the largest log files or directories on fixed asset 203.0.113.10. "
        "Read-only: uses sudo only for find/du size inspection and never modifies files."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "default": "/var/log",
                "description": "Absolute directory to inspect. Defaults to /var/log.",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum rows to return.",
            },
            "min_size_mb": {
                "type": "integer",
                "default": 100,
                "minimum": 1,
                "description": "Only include paths larger than this size in MB.",
            },
        },
        "title": "find_large_log_pathsArguments",
    },
)


class BearerAuth(httpx.Auth):
    def __init__(self, token: str | bytes) -> None:
        self._auth_header = self._build_auth_header(token)

    def auth_flow(
        self, request: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = self._auth_header
        yield request

    def _build_auth_header(self, token: str | bytes) -> str:
        return f"Bearer {token}"


class JumpServerAccessKeyAuth(httpx.Auth):
    """JumpServer HTTP Signature auth for Access Key ID + Secret."""

    def __init__(
        self,
        key_id: str,
        secret: str | bytes,
        include_host: bool = False,
        sign_query: bool = False,
    ) -> None:
        self.key_id = key_id
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.include_host = include_host
        self.sign_query = sign_query

    def auth_flow(
        self, request: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        # Honor an explicit caller-set Accept (e.g. the swagger endpoint needs
        # application/openapi+json — JumpServer's DRF content negotiation 406s
        # on plain application/json there). httpx sets a default Accept of */*
        # on every request, so treat */* (or missing) as "no override" and fall
        # back to application/json, preserving the signed value used by every
        # normal API call. The signed accept must match the sent header exactly.
        existing_accept = request.headers.get("Accept")
        accept = (
            existing_accept
            if existing_accept and existing_accept != "*/*"
            else "application/json"
        )
        request.headers["Accept"] = accept
        request.headers["Date"] = date

        target = request.url.raw_path.decode()
        if self.sign_query and request.url.query:
            target = f"{target}?{request.url.query.decode()}"
        signature_headers = ["(request-target)", "accept", "date"]
        if self.include_host:
            request.headers["Host"] = request.url.netloc.decode()
            signature_headers.append("host")
        signing_string = (
            f"(request-target): {request.method.lower()} {target}\n"
            f"accept: {accept}\n"
            f"date: {date}"
        )
        if self.include_host:
            signing_string = f"{signing_string}\nhost: {request.headers['Host']}"
        digest = hmac.new(self.secret, signing_string.encode(), hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode()
        request.headers["Authorization"] = (
            f'Signature keyId="{self.key_id}",algorithm="hmac-sha256",'
            f'headers="{" ".join(signature_headers)}",signature="{signature}"'
        )
        yield request


def build_jumpserver_auth() -> httpx.Auth | None:
    if settings.access_key_id and settings.access_key_secret:
        return JumpServerAccessKeyAuth(
            settings.access_key_id,
            settings.access_key_secret,
            settings.access_key_include_host,
            settings.access_key_sign_query,
        )
    if settings.api_token:
        return BearerAuth(settings.api_token)
    return None


def get_swagger_json(url: str = settings.swagger_url) -> dict[str, Any]:
    # JumpServer's /api/docs/?format=openapi negotiates on Accept: it 406s on
    # application/json and only serves the schema for application/openapi+json.
    # Set it explicitly so JumpServerAccessKeyAuth signs and sends that value.
    kwargs: dict[str, Any] = {
        "verify": False,
        "timeout": 120,
        "headers": {"Accept": "application/openapi+json"},
    }
    auth = build_jumpserver_auth()
    if auth:
        kwargs["auth"] = auth
    resp = httpx.get(url, **kwargs)
    if resp.status_code != HTTP_OK:
        error_message = f"Failed to fetch OpenAPI schema: {resp.status_code} - {resp.text}"
        raise OpenAPISchemaFetchError(error_message)
    return resp.json()


def ensure_core_tools(swagger_json: dict[str, Any]) -> dict[str, Any]:
    paths = swagger_json.setdefault("paths", {})
    paginated_response = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "next": {"type": "string", "x-nullable": True},
            "previous": {"type": "string", "x-nullable": True},
            "results": {"type": "array", "items": {"type": "object"}},
        },
    }
    common_list_params = [
        {"name": "search", "in": "query", "required": False, "type": "string"},
        {"name": "limit", "in": "query", "required": False, "type": "integer"},
        {"name": "offset", "in": "query", "required": False, "type": "integer"},
    ]
    paths.setdefault(
        "/assets/assets/",
        {
            "get": {
                "operationId": "assets_assets_list",
                "summary": "List assets",
                "parameters": common_list_params,
                "responses": {"200": {"description": "", "schema": paginated_response}},
                "tags": ["assets_assets"],
            },
            "parameters": [],
        },
    )
    paths.setdefault(
        "/assets/assets/{id}/",
        {
            "get": {
                "operationId": "assets_assets_read",
                "summary": "Read asset detail",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "responses": {"200": {"description": "", "schema": {"type": "object"}}},
                "tags": ["assets_assets"],
            },
            "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
        },
    )
    paths.setdefault(
        "/assets/hosts/",
        {
            "get": {
                "operationId": "assets_hosts_list",
                "summary": "List host assets",
                "parameters": common_list_params,
                "responses": {"200": {"description": "", "schema": paginated_response}},
                "tags": ["assets_hosts"],
            },
            "parameters": [],
        },
    )
    paths.setdefault(
        "/assets/hosts/{id}/",
        {
            "get": {
                "operationId": "assets_hosts_read",
                "summary": "Read host asset detail",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "responses": {"200": {"description": "", "schema": {"type": "object"}}},
                "tags": ["assets_hosts"],
            },
            "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
        },
    )
    paths.setdefault(
        "/accounts/accounts/",
        {
            "get": {
                "operationId": "accounts_accounts_list",
                "summary": "List accounts (optionally filtered by asset)",
                "parameters": common_list_params
                + [{"name": "asset", "in": "query", "required": False, "type": "string"}],
                "responses": {"200": {"description": "", "schema": paginated_response}},
                "tags": ["accounts_accounts"],
            },
            "parameters": [],
        },
    )
    paths.setdefault(
        "/ops/jobs/",
        {
            "post": {
                "operationId": "ops_jobs_create",
                "summary": "Create and optionally run an ops job",
                "parameters": [],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "default": "adhoc"},
                                "module": {"type": "string", "default": "shell"},
                                "args": {"type": "string"},
                                "assets": {
                                    "type": "array",
                                    "items": {"type": "string", "format": "uuid"},
                                },
                                "runas_policy": {"type": "string", "default": "skip"},
                                "runas": {"type": "string"},
                                "timeout": {"type": "integer"},
                                "chdir": {"type": "string"},
                                "instant": {"type": "boolean"},
                                "use_parameter_define": {"type": "boolean", "default": False},
                                "is_periodic": {"type": "boolean", "default": False},
                                "run_after_save": {"type": "boolean"},
                                "comment": {"type": "string"},
                            },
                        },
                        }
                    },
                },
                "responses": {"201": {"description": "", "schema": {"type": "object"}}},
                "tags": ["ops_jobs"],
            },
            "parameters": [],
        },
    )
    paths.setdefault(
        "/ops/job-execution/task-detail/{task_id}/",
        {
            "get": {
                "operationId": "ops_job_execution_task_detail_read",
                "summary": "Read ops job execution task detail",
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "type": "string"}
                ],
                "responses": {"200": {"description": "", "schema": {"type": "object"}}},
                "tags": ["ops_job_execution"],
            },
            "parameters": [{"name": "task_id", "in": "path", "required": True, "type": "string"}],
        },
    )
    return swagger_json


class JumpServerOpenapiMCP(FastApiMCP):
    def __init__(self, app: FastAPI, **kwargs: Any) -> None:
        self.swagger_json = kwargs.pop("swagger_json")
        self.jumpserver_auth = kwargs.pop("jumpserver_auth", None)
        super().__init__(app, **kwargs)

    def setup_server(self) -> None:
        openapi_schema = self.swagger_json
        all_tools, self.operation_map = convert_openapi_to_mcp_tools(
            openapi_schema,
            describe_all_responses=self._describe_all_responses,
            describe_full_response_schema=self._describe_full_response_schema,
        )
        logger.info("Loaded %d tools from OpenAPI schema.", len(all_tools))
        self.tools = self._filter_tools(all_tools, openapi_schema)
        logger.info("Filtered to %d tools after applying filters.", len(self.tools))
        self._base_url = self._base_url.removesuffix("/")

        mcp_server: Server = Server(self.name, self.description)

        tool_context = ToolContext(
            base_url=self.fastapi.root_path or base_url,
            auth=self.jumpserver_auth,
        )
        self.tool_context = tool_context

        @mcp_server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return self.tools + [FIND_LARGE_LOG_TOOL] + HANDWRITTEN_TOOLS

        @mcp_server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            if name in HANDWRITTEN_TOOL_NAMES:
                return await tool_context.dispatch(name, arguments)
            if name == FIND_LARGE_LOG_TOOL.name:
                safe_args = validate_large_log_args(arguments)
                job_arguments = {
                    "name": "mcp-find-large-log-paths-203-0-113-10",
                    "type": "adhoc",
                    "module": "shell",
                    "args": build_large_log_command(safe_args),
                    "assets": [TARGET_ASSET_ID],
                    "runas_policy": "privileged_first",
                    "runas": TARGET_RUNAS,
                    "timeout": 120,
                    "instant": True,
                    "run_after_save": True,
                    "use_parameter_define": False,
                    "is_periodic": False,
                    "comment": "read-only MCP log size inspection",
                }
                return await self._execute_large_log_tool(job_arguments)
            # JumpServer auth is built from settings (Access Key or Bearer) and
            # is what authenticates downstream API calls. The old SSE transport
            # additionally threaded the client's Authorization header through
            # experimental_capabilities["session_token"]; the Streamable HTTP
            # session manager owns initialization, so that path is gone. The
            # gateway API key is not a JumpServer credential anyway, so relying
            # on self.jumpserver_auth is both correct and simpler.
            http_client = httpx.AsyncClient(
                base_url=self.fastapi.root_path or base_url,
                verify=False,
                auth=self.jumpserver_auth,
                timeout=60,
            )
            return await self._execute_api_tool(
                client=http_client,
                tool_name=name,
                arguments=arguments,
                operation_map=self.operation_map,
            )

        self.server = mcp_server

    async def _execute_large_log_tool(
        self, job_arguments: dict[str, Any]
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        async with httpx.AsyncClient(
            base_url=self.fastapi.root_path or base_url,
            verify=False,
            auth=self.jumpserver_auth,
            timeout=120,
        ) as http_client:
            create_resp = await http_client.post("/ops/jobs/", json=job_arguments)
            if create_resp.status_code >= 400:
                raise Exception(
                    "Error calling find_large_log_paths. "
                    f"Status code: {create_resp.status_code}. Response: {create_resp.text}"
                )
            create_data = create_resp.json()
            task_id = create_data.get("task_id")
            detail_data: dict[str, Any] = {}
            if task_id:
                for _ in range(20):
                    detail_resp = await http_client.get(f"/ops/job-execution/task-detail/{task_id}/")
                    if detail_resp.status_code >= 400:
                        raise Exception(
                            "Error reading find_large_log_paths task detail. "
                            f"Status code: {detail_resp.status_code}. Response: {detail_resp.text}"
                        )
                    detail_data = detail_resp.json()
                    if detail_data.get("is_finished"):
                        break
                    await asyncio.sleep(1)

            result = {
                "target_asset": "203.0.113.10",
                "task_id": task_id,
                "job_id": detail_data.get("job_id"),
                "status": detail_data.get("status"),
                "is_finished": detail_data.get("is_finished"),
                "is_success": True,
                "summary": detail_data.get("summary"),
                "results": parse_large_log_output(detail_data.get("summary") or {}),
            }
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


app = FastAPI()
jumpserver_url = settings.jumpserver_url.rstrip("/")
base_url = settings.api_base_url
if not base_url and jumpserver_url:
    base_url = f"{jumpserver_url}/api/v1"
swagger_url = settings.swagger_url
if not swagger_url and jumpserver_url:
    swagger_url = f"{jumpserver_url}/api/docs/?format=openapi"

logger.info("Fetching OpenAPI schema from API URL: %s", swagger_url)
swagger_json = ensure_core_tools(get_swagger_json(swagger_url))
jumpserver_auth = build_jumpserver_auth()

mcp = JumpServerOpenapiMCP(
    app,
    name="JumpServer API MCP",
    describe_all_responses=True,
    describe_full_response_schema=True,
    http_client=httpx.AsyncClient(base_url=base_url, auth=jumpserver_auth, verify=False),
    swagger_json=swagger_json,
    jumpserver_auth=jumpserver_auth,
)
mount_path = settings.base_path.strip('"').strip("'")
if not mount_path.startswith("/"):
    mount_path = "/" + mount_path
mcp.mount_http(mount_path=mount_path)


@app.middleware("http")
async def check_api_key(request: Request, call_next) -> Response:
    # Streamable HTTP gates every request on the Bearer key. Unlike the old SSE
    # transport there is no `?session_id=` side channel to special-case: session
    # continuity is carried by the `Mcp-Session-Id` header, which the transport
    # validates itself, so we only enforce the API key here and let unknown or
    # expired sessions be rejected downstream by the transport.
    if settings.api_key:
        api_key = request.headers.get("Authorization")
        if (
            not api_key
            or not api_key.startswith("Bearer ")
            or api_key != f"Bearer {settings.api_key}"
        ):
            return Response(status_code=401, content="Unauthorized: Invalid API token")
    return await call_next(request)
