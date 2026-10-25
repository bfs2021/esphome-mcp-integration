"""Tests for the atomic _write_file helper shared by the write/create/patch tools."""
from __future__ import annotations

import os
import stat
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.esphome_mcp_bridge.llm import _write_file  # noqa: E402


def test_creates_parent_dirs_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "components" / "x" / "sensor.h"
    _write_file(str(target), "a\n")
    _write_file(str(target), "b\n")
    assert target.read_text() == "b\n"
    assert os.listdir(target.parent) == ["sensor.h"]


def test_preserves_existing_mode(tmp_path):
    target = tmp_path / "x.yaml"
    target.write_text("old\n")
    target.chmod(0o640)
    _write_file(str(target), "new\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_writes_through_symlink(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("old\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    _write_file(str(link), "new\n")
    assert link.is_symlink()
    assert real.read_text() == "new\n"


def test_failed_write_keeps_original_and_cleans_up(tmp_path):
    target = tmp_path / "x.yaml"
    target.write_text("original\n")
    with mock.patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            _write_file(str(target), "new\n")
    assert target.read_text() == "original\n"
    assert os.listdir(tmp_path) == ["x.yaml"]
