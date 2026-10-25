"""Client for the Home Assistant Supervisor API.

Used to discover installed ESPHome add-ons (stable / beta / dev) and to open a
connection to each add-on's dashboard.

The modern ESPHome Device Builder add-on guards its ingress listener with a TCP
peer allowlist (loopback + the Supervisor at ``172.30.32.2``), so hitting the
add-on's ``hostname:ingress_port`` directly from the HA Core container now gets
a 403. We therefore route dashboard traffic through the Supervisor's ingress
proxy (``/ingress/<token>/...``): the request reaches the add-on *from* the
Supervisor, satisfying the peer guard, and the trusted-ingress site skips auth
because the Supervisor already authenticated us. See
:meth:`SupervisorClient.async_dashboard_target`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from .const import (
    CHANNEL_SUFFIXES,
    DEFAULT_DASHBOARD_PORT,
    ESPHOME_SLUG_MATCH,
    SUPERVISOR_BASE_URL,
)
from .exceptions import AddonNotFoundError, SupervisorError

_LOGGER = logging.getLogger(__name__)


@dataclass
class AddonInfo:
    """Summary of an installed ESPHome add-on."""

    slug: str
    name: str
    version: str | None
    version_latest: str | None
    state: str | None
    update_available: bool

    @property
    def is_running(self) -> bool:
        return self.state == "started"


@dataclass
class DashboardTarget:
    """Cacheable, session-independent way to reach an add-on's dashboard.

    ``ingress_token`` is the add-on's stable ingress token when the dashboard is
    reached through the Supervisor ingress proxy, or ``None`` when we fall back
    to direct ``hostname:port`` access (a classic dashboard with no peer guard).
    """

    base_url: str
    ingress_token: str | None


@dataclass
class DashboardConnection:
    """A ready-to-use dashboard connection: base URL plus per-request headers.

    ``headers`` carries the ``ingress_session`` cookie for proxied targets and
    is empty for direct ones. It must be applied to every REST and WebSocket
    request the dashboard client makes.
    """

    base_url: str
    headers: dict[str, str]


class SupervisorClient:
    """Thin async wrapper over the Supervisor REST API.

    ``session`` is an externally owned :class:`aiohttp.ClientSession` (in Home
    Assistant, the shared session). ``token`` is the value of the
    ``SUPERVISOR_TOKEN`` environment variable injected into HA Core.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        base_url: str = SUPERVISOR_BASE_URL,
    ) -> None:
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        """Call a Supervisor endpoint and return the unwrapped ``data`` object.

        On failure the Supervisor's own ``message`` is included in the error
        (it names e.g. a missing request field), not just the HTTP status.
        """
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=json,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
                if resp.status >= 400:
                    detail = (
                        payload.get("message")
                        if isinstance(payload, dict) and payload.get("message")
                        else resp.reason
                    )
                    raise SupervisorError(
                        f"Supervisor {method} {path} failed: {resp.status} {detail}"
                    )
        except aiohttp.ClientError as err:
            raise SupervisorError(f"Supervisor {method} {path} failed: {err}") from err

        if not isinstance(payload, dict) or payload.get("result") != "ok":
            message = payload.get("message", payload) if isinstance(payload, dict) else payload
            raise SupervisorError(f"Supervisor {method} {path} returned: {message}")
        return payload.get("data", {})

    async def _get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def _post(self, path: str, json: dict | None = None) -> dict:
        return await self._request("POST", path, json if json is not None else {})

    async def list_esphome_addons(self) -> list[AddonInfo]:
        """Return all installed add-ons that look like ESPHome dashboards."""
        data = await self._get("/addons")
        addons = data.get("addons", [])
        result: list[AddonInfo] = []
        for addon in addons:
            slug = addon.get("slug", "")
            name = addon.get("name", "")
            if ESPHOME_SLUG_MATCH in slug.lower() or ESPHOME_SLUG_MATCH in name.lower():
                result.append(
                    AddonInfo(
                        slug=slug,
                        name=name,
                        version=addon.get("version"),
                        version_latest=addon.get("version_latest"),
                        state=addon.get("state"),
                        update_available=bool(addon.get("update_available")),
                    )
                )
        return result

    async def get_addon_info(self, slug: str) -> dict:
        """Return the full info object for a single add-on."""
        try:
            return await self._get(f"/addons/{slug}/info")
        except SupervisorError as err:
            # Supervisor returns a non-ok result (often 400/404) for unknown slugs.
            raise AddonNotFoundError(f"Add-on '{slug}' not found: {err}") from err

    async def async_default_slug(self) -> str:
        """Pick a sensible default add-on, preferring stable > beta > dev.

        Within a channel, a running add-on is preferred over a stopped one.
        """
        addons = await self.list_esphome_addons()
        if not addons:
            raise AddonNotFoundError("No ESPHome add-on is installed.")

        def rank(addon: AddonInfo) -> tuple[int, int]:
            channel = next(
                (i for i, suffix in enumerate(CHANNEL_SUFFIXES) if suffix and addon.slug.endswith(suffix)),
                0,  # no suffix -> stable -> highest priority
            )
            return (channel, 0 if addon.is_running else 1)

        return sorted(addons, key=rank)[0].slug

    async def async_dashboard_target(self, slug: str) -> DashboardTarget:
        """Resolve how to reach an add-on's dashboard (stable / cacheable).

        Preferred path: the Supervisor ingress proxy. The add-on's info exposes
        ``ingress_entry`` as ``/api/hassio_ingress/<token>``; the proxy route is
        ``/ingress/<token>/...`` on the Supervisor. Routing through it makes the
        add-on see the Supervisor as the TCP peer, which satisfies the Device
        Builder's ingress peer guard (loopback + Supervisor only).

        Fallback: if the add-on doesn't advertise ingress, hit its container
        ``hostname:ingress_port`` directly (a classic dashboard with no guard).

        The returned target has no per-request auth of its own; call
        :meth:`open_dashboard_connection` to mint an ingress session for it.
        """
        info = await self.get_addon_info(slug)
        entry = info.get("ingress_entry")
        if entry:
            # ``entry`` is ``/api/hassio_ingress/<token>``; the token is its
            # last path segment.
            token = entry.rstrip("/").rsplit("/", 1)[-1]
            if token:
                return DashboardTarget(
                    base_url=f"{self._base_url}/ingress/{token}",
                    ingress_token=token,
                )

        hostname = info.get("hostname") or slug.replace("_", "-")
        port = info.get("ingress_port") or DEFAULT_DASHBOARD_PORT
        return DashboardTarget(base_url=f"http://{hostname}:{port}", ingress_token=None)

    async def create_ingress_session(self, user_id: str) -> str:
        """Create a Supervisor ingress session for a HA user and return its id.

        Since Supervisor 2026.10 the session must name an active Home Assistant
        user (``user_id``); a body without it is rejected with 400. For apps
        that set ``panel_admin`` (ESPHome does), the proxy additionally answers
        403 unless that user is an admin.

        The id must be sent back as the ``ingress_session`` cookie on every
        proxied request; the proxy validates it (and extends its TTL) per call.
        """
        data = await self._post("/ingress/session", {"user_id": user_id})
        session = data.get("session")
        if not session:
            raise SupervisorError("Supervisor did not return an ingress session")
        return session

    async def open_dashboard_connection(
        self, target: DashboardTarget, user_id: str | None = None
    ) -> DashboardConnection:
        """Turn a (cached) :class:`DashboardTarget` into a live connection.

        For proxied targets this mints a fresh ingress session owned by
        ``user_id`` so a long-lived cache never hands out an expired one;
        direct targets need no auth and ignore ``user_id``.
        """
        if target.ingress_token is None:
            return DashboardConnection(base_url=target.base_url, headers={})
        if not user_id:
            raise SupervisorError(
                "Reaching the ESPHome dashboard through Supervisor ingress "
                "requires the Home Assistant user making the request, but none "
                "was provided."
            )
        session = await self.create_ingress_session(user_id)
        return DashboardConnection(
            base_url=target.base_url,
            headers={"Cookie": f"ingress_session={session}"},
        )
