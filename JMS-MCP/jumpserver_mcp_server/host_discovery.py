"""Host discovery over JumpServer's ``/assets/hosts/`` endpoint.

Wraps the (already-registered) hosts list/read API with:
- hostname search (``search=``)
- IP-address search (``search=`` also matches address in JumpServer)
- additional filtering by OS / asset type / group (client-side narrowing on
  fields JumpServer's list endpoint does not always filter natively)
- a short-lived in-memory cache to avoid hammering the API for repeat lookups

The discovery tool returns, per host, the asset ``id`` plus the candidate
``runas`` accounts — exactly what the user-connection and session layers need
to dispatch an ops job.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

import httpx

from .config import settings
from .errors import JumpServerUnreachableError

logger = getLogger(__name__)

HTTP_BAD_REQUEST = 400
HTTP_SERVER_ERROR = 500


@dataclass
class _CacheEntry:
    value: list[dict[str, Any]]
    expires_at: float


class HostDiscovery:
    """Query and filter JumpServer host assets."""

    def __init__(self, client: httpx.AsyncClient, cache_ttl: float | None = None) -> None:
        self._client = client
        self._cache_ttl = (
            cache_ttl if cache_ttl is not None else settings.host_cache_ttl_seconds
        )
        self._cache: dict[str, _CacheEntry] = {}

    def _cache_get(self, key: str) -> list[dict[str, Any]] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return entry.value

    def _cache_put(self, key: str, value: list[dict[str, Any]]) -> None:
        self._cache[key] = _CacheEntry(
            value=value, expires_at=time.monotonic() + self._cache_ttl
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    @staticmethod
    def is_valid_ip_query(query: str) -> bool:
        """True if query is a single IP or CIDR subnet."""
        try:
            if "/" in query:
                ipaddress.ip_network(query, strict=False)
            else:
                ipaddress.ip_address(query)
        except ValueError:
            return False
        return True

    async def _list_hosts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Call ``/assets/hosts/`` and return the results array.

        Handles both paginated ({count,results}) and bare-list responses.
        """
        cache_key = repr(sorted(params.items()))
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = await self._client.get("/assets/hosts/", params=params)
        except httpx.TransportError as exc:
            raise JumpServerUnreachableError(
                "JumpServer unreachable while listing hosts",
                detail={"cause": repr(exc)},
            ) from exc
        if resp.status_code >= HTTP_SERVER_ERROR:
            raise JumpServerUnreachableError(
                f"JumpServer returned {resp.status_code} listing hosts",
                detail={"status_code": resp.status_code},
            )
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise JumpServerUnreachableError(
                f"Unexpected {resp.status_code} listing hosts",
                detail={"status_code": resp.status_code, "body": resp.text[:500]},
            )
        data = resp.json()
        if isinstance(data, dict):
            results = data.get("results") or []
        else:
            results = data or []
        self._cache_put(cache_key, results)
        return results

    @staticmethod
    def _host_address(host: dict[str, Any]) -> str | None:
        return host.get("address") or host.get("ip") or None

    @staticmethod
    def _matches_filters(
        host: dict[str, Any],
        *,
        os_type: str | None,
        asset_type: str | None,
        group: str | None,
    ) -> bool:
        if os_type:
            platform = host.get("platform") or {}
            host_os = ""
            if isinstance(platform, dict):
                host_os = str(
                    platform.get("type") or platform.get("os") or platform.get("name") or ""
                )
            host_os = host_os or str(host.get("os_type") or "")
            if os_type.lower() not in host_os.lower():
                return False
        if asset_type:
            category = host.get("category") or {}
            type_field = host.get("type") or {}
            cat = category.get("value", category) if isinstance(category, dict) else category
            typ = type_field.get("value", type_field) if isinstance(type_field, dict) else type_field
            haystack = f"{cat} {typ}".lower()
            if asset_type.lower() not in haystack:
                return False
        if group:
            nodes = host.get("nodes_display") or host.get("nodes") or []
            joined = " ".join(str(n) for n in nodes).lower()
            if group.lower() not in joined:
                return False
        return True

    async def search_hosts(
        self,
        query: str | None = None,
        *,
        os_type: str | None = None,
        asset_type: str | None = None,
        group: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search hosts by hostname or IP, then apply client-side filters.

        ``query`` is passed to JumpServer's ``search=`` param, which matches
        both name and address. Returns normalized host summaries with the
        asset id and candidate runas accounts.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if query:
            params["search"] = query
        raw_hosts = await self._list_hosts(params)

        # If the query is a subnet, narrow to addresses inside it.
        subnet: ipaddress._BaseNetwork | None = None
        if query and "/" in query and self.is_valid_ip_query(query):
            subnet = ipaddress.ip_network(query, strict=False)

        results: list[dict[str, Any]] = []
        for host in raw_hosts:
            if not self._matches_filters(
                host, os_type=os_type, asset_type=asset_type, group=group
            ):
                continue
            if subnet is not None:
                addr = self._host_address(host)
                try:
                    if addr is None or ipaddress.ip_address(addr) not in subnet:
                        continue
                except ValueError:
                    continue
            results.append(self.summarize_host(host))
        return results

    @staticmethod
    def summarize_host(host: dict[str, Any]) -> dict[str, Any]:
        """Reduce a raw host asset to the fields callers need.

        ``runas_candidates`` is left empty here — the hosts list endpoint only
        reports ``accounts_amount``, not the accounts themselves. Use
        ``fetch_runas_candidates(asset_id)`` (or ``search_hosts_with_accounts``)
        to populate it from ``/accounts/accounts/``.
        """
        platform = host.get("platform") or {}
        if isinstance(platform, dict):
            platform_name = platform.get("name")
            platform_os = platform.get("type") or platform.get("os")
        else:
            platform_name = platform
            platform_os = None
        type_field = host.get("type") or {}
        host_type = (
            type_field.get("value") if isinstance(type_field, dict) else type_field
        )
        return {
            "id": host.get("id"),
            "name": host.get("name"),
            "address": HostDiscovery._host_address(host),
            "platform": platform_name,
            "os_type": platform_os or host_type or host.get("os_type"),
            "asset_type": host_type,
            "nodes": host.get("nodes_display") or [],
            "accounts_amount": host.get("accounts_amount"),
            "comment": host.get("comment"),
            "runas_candidates": [],
        }

    @staticmethod
    def summarize_account(account: dict[str, Any]) -> dict[str, Any]:
        """Reduce a raw account record to the runas fields callers need."""
        secret_type = account.get("secret_type")
        if isinstance(secret_type, dict):
            secret_type = secret_type.get("value")
        return {
            "id": account.get("id"),
            "username": account.get("username"),
            "name": account.get("name"),
            "privileged": account.get("privileged"),
            "secret_type": secret_type,
        }

    async def fetch_runas_candidates(self, asset_id: str) -> list[dict[str, Any]]:
        """List the accounts (runas candidates) configured for an asset."""
        cache_key = f"accounts:{asset_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._client.get(
                "/accounts/accounts/", params={"asset": asset_id, "limit": 100}
            )
        except httpx.TransportError as exc:
            raise JumpServerUnreachableError(
                "JumpServer unreachable while listing accounts",
                detail={"cause": repr(exc), "asset_id": asset_id},
            ) from exc
        if resp.status_code >= HTTP_SERVER_ERROR:
            raise JumpServerUnreachableError(
                f"JumpServer returned {resp.status_code} listing accounts",
                detail={"status_code": resp.status_code},
            )
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise JumpServerUnreachableError(
                f"Unexpected {resp.status_code} listing accounts",
                detail={"status_code": resp.status_code, "body": resp.text[:500]},
            )
        data = resp.json()
        raw_accounts = data.get("results") if isinstance(data, dict) else data
        candidates = [
            self.summarize_account(acc)
            for acc in (raw_accounts or [])
            if isinstance(acc, dict)
        ]
        self._cache_put(cache_key, candidates)
        return candidates

    async def search_hosts_with_accounts(
        self,
        query: str | None = None,
        *,
        os_type: str | None = None,
        asset_type: str | None = None,
        group: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """``search_hosts`` plus populated ``runas_candidates`` per host."""
        hosts = await self.search_hosts(
            query,
            os_type=os_type,
            asset_type=asset_type,
            group=group,
            limit=limit,
            offset=offset,
        )
        for host in hosts:
            asset_id = host.get("id")
            if asset_id and host.get("accounts_amount"):
                host["runas_candidates"] = await self.fetch_runas_candidates(asset_id)
        return hosts

    async def get_host(self, asset_id: str) -> dict[str, Any] | None:
        """Read a single host asset by id; returns a normalized summary."""
        try:
            resp = await self._client.get(f"/assets/hosts/{asset_id}/")
        except httpx.TransportError as exc:
            raise JumpServerUnreachableError(
                "JumpServer unreachable while reading host",
                detail={"cause": repr(exc), "asset_id": asset_id},
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise JumpServerUnreachableError(
                f"Unexpected {resp.status_code} reading host {asset_id}",
                detail={"status_code": resp.status_code},
            )
        return self.summarize_host(resp.json())
