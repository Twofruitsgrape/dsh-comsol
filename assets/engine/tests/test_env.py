"""Unit tests for environment helpers (no COMSOL installation required)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from comsol_mcp import env
from comsol_mcp.errors import ComsolMcpError


def test_version_key_orders_numerically():
    assert env._version_key("6.2") == (6, 2)
    assert env._version_key("6.10") > env._version_key("6.9")
    assert env._version_key("unknown") < env._version_key("6.0")


def test_install_from_root_requires_server_executable(tmp_path: Path):
    root = tmp_path / "COMSOL62" / "Multiphysics"
    assert env._install_from_root(root) is None

    bin_dir = root / ("bin/win64" if env.IS_WINDOWS else "bin")
    bin_dir.mkdir(parents=True)
    suffix = ".exe" if env.IS_WINDOWS else ""
    (bin_dir / f"comsolmphserver{suffix}").write_text("", encoding="utf-8")
    (bin_dir / f"comsolmphclient{suffix}").write_text("", encoding="utf-8")

    install = env._install_from_root(root)
    assert install is not None
    assert install.server_exe.name == f"comsolmphserver{suffix}"
    assert install.client_exe is not None
    assert install.gui_exe is None  # comsol.exe was not created


def test_find_installations_honours_comsol_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "Multiphysics"
    bin_dir = root / ("bin/win64" if env.IS_WINDOWS else "bin")
    bin_dir.mkdir(parents=True)
    suffix = ".exe" if env.IS_WINDOWS else ""
    (bin_dir / f"comsolmphserver{suffix}").write_text("", encoding="utf-8")
    monkeypatch.setenv("COMSOL_ROOT", str(root))

    installs = env.find_installations()
    assert [install.root for install in installs] == [root]


def test_pick_installation_reports_available_versions(monkeypatch):
    fake = [
        env.Install("6.1", Path("/a"), Path("/a/server"), None, None),
        env.Install("6.2", Path("/b"), Path("/b/server"), None, None),
    ]
    monkeypatch.setattr(env, "find_installations", lambda: fake)

    assert env.pick_installation().version == "6.2"          # newest by default
    assert env.pick_installation("6.1").version == "6.1"     # exact match
    assert env.pick_installation("6").version == "6.2"       # prefix match

    with pytest.raises(ComsolMcpError) as excinfo:
        env.pick_installation("5.6")
    assert "6.1" in str(excinfo.value) and "6.2" in str(excinfo.value)


def test_pick_installation_without_any_install(monkeypatch):
    monkeypatch.setattr(env, "find_installations", list)
    with pytest.raises(ComsolMcpError) as excinfo:
        env.pick_installation()
    assert "COMSOL_ROOT" in (excinfo.value.hint or "")


def test_port_helpers_roundtrip():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        # a generous backlog: the probes never accept(), and a full backlog
        # makes further connection attempts fail on Windows
        server.listen(16)
        port = server.getsockname()[1]
        assert env.port_open(port)
        assert env.wait_for_port(port, timeout=2.0, interval=0.1)
    assert not env.port_open(port, timeout=0.2)
    assert not env.wait_for_port(port, timeout=0.5, interval=0.1)
