"""Tests for host discovery (task 9.1) using respx-mocked JumpServer endpoints."""

import httpx
import pytest
import respx

from jumpserver_mcp_server.host_discovery import HostDiscovery

BASE = "http://jms.test/api/v1"

_HOST = {
    "id": "asset-1",
    "name": "web-01",
    "address": "10.0.0.5",
    "platform": {"id": 1, "name": "Linux", "type": "linux"},
    "type": {"value": "linux", "label": "Linux"},
    "nodes_display": ["/Default/prod"],
    "accounts_amount": 1,
    "comment": "",
}

_ACCOUNT = {
    "id": "acc-1",
    "name": "ec2-user",
    "username": "ec2-user",
    "privileged": False,
    "secret_type": {"value": "ssh_key", "label": "SSH key"},
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE)


def test_is_valid_ip_query():
    assert HostDiscovery.is_valid_ip_query("10.0.0.5")
    assert HostDiscovery.is_valid_ip_query("10.0.0.0/24")
    assert not HostDiscovery.is_valid_ip_query("web-01")


def test_summarize_host_maps_fields():
    summary = HostDiscovery.summarize_host(_HOST)
    assert summary["id"] == "asset-1"
    assert summary["os_type"] == "linux"
    assert summary["asset_type"] == "linux"
    assert summary["platform"] == "Linux"
    assert summary["runas_candidates"] == []


@pytest.mark.asyncio
@respx.mock
async def test_search_hosts_by_name():
    respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [_HOST]})
    )
    async with _client() as client:
        disc = HostDiscovery(client, cache_ttl=0)
        hosts = await disc.search_hosts("web-01")
    assert len(hosts) == 1
    assert hosts[0]["name"] == "web-01"


@pytest.mark.asyncio
@respx.mock
async def test_search_hosts_subnet_filter():
    other = dict(_HOST, id="asset-2", name="db-01", address="192.168.1.9")
    respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"count": 2, "results": [_HOST, other]})
    )
    async with _client() as client:
        disc = HostDiscovery(client, cache_ttl=0)
        hosts = await disc.search_hosts("10.0.0.0/24")
    # Only the 10.0.0.5 host is inside the subnet.
    assert [h["address"] for h in hosts] == ["10.0.0.5"]


@pytest.mark.asyncio
@respx.mock
async def test_search_hosts_os_filter_excludes():
    win = dict(
        _HOST, id="w", name="win", platform={"name": "Windows", "type": "windows"},
        type={"value": "windows"},
    )
    respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"results": [_HOST, win]})
    )
    async with _client() as client:
        disc = HostDiscovery(client, cache_ttl=0)
        hosts = await disc.search_hosts(None, os_type="linux")
    assert [h["name"] for h in hosts] == ["web-01"]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_runas_candidates():
    respx.get(f"{BASE}/accounts/accounts/").mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [_ACCOUNT]})
    )
    async with _client() as client:
        disc = HostDiscovery(client, cache_ttl=0)
        accounts = await disc.fetch_runas_candidates("asset-1")
    assert accounts == [
        {
            "id": "acc-1",
            "username": "ec2-user",
            "name": "ec2-user",
            "privileged": False,
            "secret_type": "ssh_key",
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_cache_avoids_second_call():
    route = respx.get(f"{BASE}/assets/hosts/").mock(
        return_value=httpx.Response(200, json={"results": [_HOST]})
    )
    async with _client() as client:
        disc = HostDiscovery(client, cache_ttl=60)
        await disc.search_hosts("web-01")
        await disc.search_hosts("web-01")
    assert route.call_count == 1
