"""Tests for direct-to-device log streaming (aioesphomeapi), using a fake client."""
import pytest

from esphome_mcp_client import DeviceLogError, async_stream_device_logs


class FakeMsg:
    def __init__(self, message):
        self.message = message


class FakeClient:
    """Stands in for aioesphomeapi.APIClient. subscribe_logs delivers canned
    messages synchronously; disconnect is recorded for cleanup assertions."""

    def __init__(self, logs=(), fail_connect=False):
        self._logs = list(logs)
        self.fail_connect = fail_connect
        self.connected = False
        self.disconnected = False
        self.unsubscribed = False

    async def connect(self, login=False):
        if self.fail_connect:
            raise RuntimeError("connection refused")
        self.connected = True

    def subscribe_logs(self, on_log, log_level=None, dump_config=None):
        self.subscribe_args = (log_level, dump_config)
        for msg in self._logs:
            on_log(msg)

        def _unsub():
            self.unsubscribed = True

        return _unsub

    async def disconnect(self):
        self.disconnected = True


async def test_collects_lines_and_disconnects():
    client = FakeClient(logs=[FakeMsg(b"line1\nline2\n"), FakeMsg(b"line3")])
    result = await async_stream_device_logs(
        "1.2.3.4", max_lines=10, max_seconds=0.05, client_factory=lambda: client
    )
    assert result.lines == ["line1", "line2", "line3"]
    assert result.success is True
    assert client.disconnected is True
    assert client.unsubscribed is True


async def test_max_lines_truncates_promptly():
    logs = [FakeMsg(f"l{i}".encode()) for i in range(10)]
    result = await async_stream_device_logs(
        "1.2.3.4", max_lines=3, max_seconds=30, client_factory=lambda: client_with(logs)
    )
    assert result.truncated is True
    assert len(result.lines) >= 3


def client_with(logs):
    return FakeClient(logs=logs)


async def test_connect_failure_raises_and_still_disconnects():
    client = FakeClient(fail_connect=True)
    with pytest.raises(DeviceLogError):
        await async_stream_device_logs(
            "1.2.3.4", max_seconds=0.05, client_factory=lambda: client
        )
    assert client.disconnected is True


async def test_subscribes_very_verbose_without_the_config_dump():
    """LOG_LEVEL_NONE (the protobuf default) is why a device capture was empty."""
    client = FakeClient(logs=[FakeMsg(b"line")])
    await async_stream_device_logs(
        "1.2.3.4", max_seconds=0.05, client_factory=lambda: client
    )
    assert client.subscribe_args == (6, False)


async def test_an_empty_capture_says_why():
    client = FakeClient(logs=[])
    result = await async_stream_device_logs(
        "1.2.3.4", max_seconds=0.05, client_factory=lambda: client
    )
    assert result.success is True
    assert len(result.lines) == 1
    assert "no log lines received" in result.lines[0]
    assert "logger: level" in result.lines[0]


async def test_handles_str_messages():
    client = FakeClient(logs=[FakeMsg("plain string line")])
    result = await async_stream_device_logs(
        "1.2.3.4", max_seconds=0.05, client_factory=lambda: client
    )
    assert "plain string line" in result.lines
