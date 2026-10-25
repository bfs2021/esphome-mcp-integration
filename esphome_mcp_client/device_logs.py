"""Direct-to-device log streaming over the ESPHome native API.

Bypasses the ESPHome dashboard entirely - so it works whether the add-on runs
the classic dashboard or the new Device Builder - by connecting to a device's
native API (port 6053) with ``aioesphomeapi`` and subscribing to its log stream
for a bounded window.

``aioesphomeapi`` is imported lazily and kept out of the package's hard
dependencies: Home Assistant already ships a pinned copy, and pinning our own
could conflict with it. Inside HA it is always available.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .dashboard import CommandResult
from .exceptions import DeviceLogError

try:  # aioesphomeapi is optional; HA always ships a compatible copy.
    from aioesphomeapi import LogLevel
except ImportError:  # pragma: no cover - only when streaming is unavailable
    LogLevel = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

#: Subscribe at the most verbose level; the device caps the stream at its
#: compiled ``logger:`` level. Without an explicit level the protobuf
#: default (0 = LOG_LEVEL_NONE) asks for no logs at all.
_DEVICE_LOG_LEVEL = (
    int(LogLevel.LOG_LEVEL_VERY_VERBOSE) if LogLevel is not None else 6
)

DEFAULT_NATIVE_PORT = 6053
DEFAULT_LOG_SECONDS = 30
DEFAULT_LOG_LINES = 500


async def async_stream_device_logs(
    address: str,
    *,
    port: int = DEFAULT_NATIVE_PORT,
    password: str | None = None,
    noise_psk: str | None = None,
    max_seconds: float = DEFAULT_LOG_SECONDS,
    max_lines: int = DEFAULT_LOG_LINES,
    client_factory: Callable[[], Any] | None = None,
) -> CommandResult:
    """Capture a bounded window of a device's native-API logs.

    Returns when ``max_seconds`` elapses or ``max_lines`` is reached, whichever
    comes first (``truncated`` is then ``True``). ``client_factory`` exists for
    testing; production builds an ``aioesphomeapi.APIClient``.
    """
    if client_factory is None:
        try:
            from aioesphomeapi import APIClient
        except ImportError as err:  # pragma: no cover - present inside HA
            raise DeviceLogError(
                "aioesphomeapi is not installed; cannot stream device logs."
            ) from err

        def client_factory() -> Any:
            return APIClient(
                address,
                port,
                password,
                noise_psk=noise_psk,
                client_info="esphome-mcp-client",
            )

    result = CommandResult()
    done = asyncio.Event()

    def _on_log(msg: Any) -> None:
        raw = getattr(msg, "message", b"")
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", "replace")
        else:
            text = str(raw)
        result.lines.extend(text.splitlines() or [text])
        if len(result.lines) >= max_lines:
            result.truncated = True
            done.set()

    client = client_factory()
    unsubscribe: Callable[[], None] | None = None
    try:
        await client.connect(login=True)
        unsubscribe = client.subscribe_logs(
            _on_log, log_level=_DEVICE_LOG_LEVEL, dump_config=False
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=max_seconds)
        except TimeoutError:
            result.truncated = True
    except DeviceLogError:
        raise
    except Exception as err:  # noqa: BLE001 - normalise any client failure
        raise DeviceLogError(f"device log stream for {address} failed: {err}") from err
    finally:
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - best-effort cleanup  # pragma: no cover
                pass
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup  # pragma: no cover
            pass

    result.exit_code = 0
    if not result.lines:
        result.lines.append(
            f"no log lines received in {max_seconds:g}s; "
            "check the device's logger: level"
        )
    return result
