"""Tests for the Supervisor client: discovery, ranking, URL resolution."""
import aiohttp
import pytest
from aiohttp import web

from esphome_mcp_client import AddonNotFoundError, SupervisorClient
from esphome_mcp_client.exceptions import SupervisorError
from esphome_mcp_client.supervisor import DashboardTarget

from .conftest import json_route, make_app

ADDONS_PAYLOAD = {
    "result": "ok",
    "data": {
        "addons": [
            {"slug": "5c53de3b_esphome-beta", "name": "ESPHome (beta)", "version": "2026.5.0b1", "state": "started"},
            {"slug": "5c53de3b_esphome", "name": "ESPHome", "version": "2026.4.0", "state": "stopped"},
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "6.5", "state": "started"},
        ]
    },
}


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


async def test_list_esphome_addons_filters_non_esphome(serve, session):
    base = await serve(make_app([("GET", "/addons", json_route(ADDONS_PAYLOAD))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    addons = await client.list_esphome_addons()
    slugs = {a.slug for a in addons}
    assert slugs == {"5c53de3b_esphome", "5c53de3b_esphome-beta"}


async def test_default_slug_prefers_stable_over_beta(serve, session):
    base = await serve(make_app([("GET", "/addons", json_route(ADDONS_PAYLOAD))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    # Stable wins even though it is stopped and beta is running.
    assert await client.async_default_slug() == "5c53de3b_esphome"


async def test_default_slug_raises_when_none_installed(serve, session):
    empty = {"result": "ok", "data": {"addons": []}}
    base = await serve(make_app([("GET", "/addons", json_route(empty))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    with pytest.raises(AddonNotFoundError):
        await client.async_default_slug()


async def test_dashboard_target_uses_ingress_proxy_when_available(serve, session):
    # An add-on with ingress advertises ``ingress_entry`` as a path whose last
    # segment is the token; the target must route through /ingress/<token>.
    info = {
        "result": "ok",
        "data": {
            "hostname": "5c53de3b-esphome",
            "ingress_port": 62513,
            "ingress_entry": "/api/hassio_ingress/aBcD-token-123",
        },
    }
    base = await serve(
        make_app([("GET", "/addons/5c53de3b_esphome/info", json_route(info))])
    )
    client = SupervisorClient(session, token="tok", base_url=base)
    target = await client.async_dashboard_target("5c53de3b_esphome")
    assert target == DashboardTarget(
        base_url=f"{base}/ingress/aBcD-token-123",
        ingress_token="aBcD-token-123",
    )


async def test_dashboard_target_falls_back_to_direct_without_ingress(serve, session):
    # No ingress_entry -> classic dashboard, reachable directly on its port.
    info = {"result": "ok", "data": {"hostname": "5c53de3b-esphome", "ingress_port": 0}}
    base = await serve(
        make_app([("GET", "/addons/5c53de3b_esphome/info", json_route(info))])
    )
    client = SupervisorClient(session, token="tok", base_url=base)
    target = await client.async_dashboard_target("5c53de3b_esphome")
    assert target == DashboardTarget(
        base_url="http://5c53de3b-esphome:6052", ingress_token=None
    )


def _session_route(seen: list):
    """Mimic Supervisor 2026.10+: ``user_id`` is required, else 400 + message."""

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append(body)
        if "user_id" not in body:
            return web.json_response(
                {"result": "error", "message": "required key not provided @ data['user_id']"},
                status=400,
            )
        return web.json_response({"result": "ok", "data": {"session": "sess-xyz"}})

    return handler


async def test_create_ingress_session_sends_user_id(serve, session):
    seen: list = []
    base = await serve(make_app([("POST", "/ingress/session", _session_route(seen))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    assert await client.create_ingress_session("user-1") == "sess-xyz"
    assert seen == [{"user_id": "user-1"}]


async def test_supervisor_error_includes_message(serve, session):
    # A 400 must surface the Supervisor's reason, not just "Bad Request".
    seen: list = []
    base = await serve(make_app([("POST", "/ingress/session", _session_route(seen))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    with pytest.raises(SupervisorError, match=r"400 required key not provided"):
        await client._post("/ingress/session")


async def test_open_connection_mints_session_cookie_for_proxy(serve, session):
    seen: list = []
    base = await serve(make_app([("POST", "/ingress/session", _session_route(seen))]))
    client = SupervisorClient(session, token="tok", base_url=base)
    target = DashboardTarget(base_url=f"{base}/ingress/tok123", ingress_token="tok123")
    conn = await client.open_dashboard_connection(target, "user-1")
    assert conn.base_url == f"{base}/ingress/tok123"
    assert conn.headers == {"Cookie": "ingress_session=sess-xyz"}
    assert seen == [{"user_id": "user-1"}]


async def test_open_connection_proxy_requires_user(session):
    client = SupervisorClient(session, token="tok", base_url="http://unused")
    target = DashboardTarget(base_url="http://unused/ingress/tok123", ingress_token="tok123")
    with pytest.raises(SupervisorError, match="requires the Home Assistant user"):
        await client.open_dashboard_connection(target, None)


async def test_open_connection_direct_target_has_no_auth(serve, session):
    # Direct targets need no ingress session; no /ingress/session call is made.
    client = SupervisorClient(session, token="tok", base_url="http://unused")
    target = DashboardTarget(base_url="http://5c53de3b-esphome:6052", ingress_token=None)
    conn = await client.open_dashboard_connection(target)
    assert conn.base_url == "http://5c53de3b-esphome:6052"
    assert conn.headers == {}
