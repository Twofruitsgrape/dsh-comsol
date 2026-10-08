"""Locating COMSOL installations, executables and server ports."""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ComsolMcpError

log = logging.getLogger(__name__)

DEFAULT_PORT = 2036
IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Install:
    version: str
    root: Path
    server_exe: Path
    client_exe: Path | None
    gui_exe: Path | None

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "root": str(self.root),
            "server_exe": str(self.server_exe),
            "client_exe": str(self.client_exe) if self.client_exe else None,
            "gui_exe": str(self.gui_exe) if self.gui_exe else None,
        }


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(version).replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _install_from_root(root: Path) -> Install | None:
    """Build an Install from a Multiphysics folder, used for COMSOL_ROOT."""
    bin_dir = root / ("bin/win64" if IS_WINDOWS else "bin")
    suffix = ".exe" if IS_WINDOWS else ""
    server = bin_dir / f"comsolmphserver{suffix}"
    if not server.exists():
        return None
    client = bin_dir / f"comsolmphclient{suffix}"
    gui = bin_dir / f"comsol{suffix}"
    return Install(
        version="unknown",
        root=root,
        server_exe=server,
        client_exe=client if client.exists() else None,
        gui_exe=gui if gui.exists() else None,
    )


def find_installations() -> list[Install]:
    """Return every COMSOL installation MPh can find, newest version first."""
    log.debug("find_installations: start")
    root_override = os.environ.get("COMSOL_ROOT")
    if root_override:
        install = _install_from_root(Path(root_override).expanduser())
        if install:
            return [install]
        log.warning("COMSOL_ROOT=%s does not look like a Multiphysics folder", root_override)

    try:
        import importlib
        import sys as _sys
        import time as _time

        for module_name in ("numpy", "jpype", "mph", "mph.discovery"):
            started = _time.monotonic()
            importlib.import_module(module_name)
            log.debug("import %s took %.2fs", module_name, _time.monotonic() - started)
        discovery = _sys.modules["mph.discovery"]
        backends = discovery.find_backends()
    except Exception as exc:
        log.warning("MPh installation discovery failed: %s", exc)
        return []

    installs: list[Install] = []
    for backend in backends:
        try:
            server_exe = Path(backend["server"][0])
            root = Path(backend["root"])
        except (KeyError, IndexError, TypeError):
            continue
        suffix = server_exe.suffix
        bin_dir = server_exe.parent
        client_exe = bin_dir / f"comsolmphclient{suffix}"
        gui_exe = bin_dir / f"comsol{suffix}"
        installs.append(
            Install(
                version=str(backend.get("name") or "unknown"),
                root=root,
                server_exe=server_exe,
                client_exe=client_exe if client_exe.exists() else None,
                gui_exe=gui_exe if gui_exe.exists() else None,
            )
        )
    installs.sort(key=lambda install: _version_key(install.version), reverse=True)
    log.debug("find_installations: done (%d found)", len(installs))
    return installs


def pick_installation(version: str | None = None) -> Install:
    installs = sorted(
        find_installations(), key=lambda install: _version_key(install.version), reverse=True
    )
    if not installs:
        raise ComsolMcpError(
            "No COMSOL Multiphysics installation was found.",
            hint="Install COMSOL 6.0 or newer (MPh supports 6.0-6.3), or point COMSOL_ROOT "
                 "at the 'Multiphysics' folder, then call comsol_detect again.",
        )
    if version:
        wanted = str(version)
        matching = sorted(
            (
                install for install in installs
                if install.version == wanted or install.version.startswith(wanted)
            ),
            key=lambda install: _version_key(install.version),
            reverse=True,
        )
        if not matching:
            available = ", ".join(sorted({i.version for i in installs}))
            raise ComsolMcpError(
                f"COMSOL {wanted} was not found (detected: {available}).",
                hint="Call comsol_detect to list installations, or omit "
                     "'version' to use the newest.",
            )
        return matching[0]
    return installs[0]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout: float = 60.0, interval: float = 1.0,
                  host: str = "127.0.0.1") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port, host):
            return True
        time.sleep(interval)
    return False
