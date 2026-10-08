"""COMSOL session management: server process, API client, visible desktop, main model.

Architecture (verified against COMSOL 6.2 in the field, see docs/findings-m0.md):

* One COMSOL Multiphysics server process holds the models in memory. It is
  started with `-multi on` so it survives client disconnects.
* This MCP server attaches as a regular COMSOL API client (through MPh).
* A COMSOL desktop client (`comsolmphclient`) connects to the *same* server.
  A model opened by the desktop lives on the server, and changes made by the
  MCP client show up in the desktop in real time. That is what makes the work
  visible to the user.

The desktop only displays models it opened itself. Therefore the GUI owns the
main model: we launch `comsolmphclient ... -open <file>` and then attach to the
resulting model tag, instead of loading the file through the API.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import desktop, env
from .errors import ComsolMcpError, ModelError, SessionError
from .progress import ProgressStream
from .snapshot import SnapshotManager, save_copy

log = logging.getLogger(__name__)

GUI_WAIT_TIMEOUT = float(os.environ.get("COMSOL_MCP_GUI_TIMEOUT", "180"))
SERVER_WAIT_TIMEOUT = float(os.environ.get("COMSOL_MCP_SERVER_TIMEOUT", "120"))
MODEL_READY_TIMEOUT = float(os.environ.get("COMSOL_MCP_READY_TIMEOUT", "150"))
PROGRESS_MODES = ("stream", "desktop", "off")


_PREPARED_CLIENT: Any = None


def prepare_client() -> bool:
    """Create the MPh client (starting the JVM) in the *main* thread.

    Must run before the MCP server starts serving requests. On Windows, loading
    the JVM (and numpy's C extension) for the first time inside the request
    worker thread hung while an MCP client was waiting - reproduced minimally,
    see docs/findings-m0.md. Doing it up front costs a couple of seconds at
    startup and makes every tool call fast and predictable.

    Returns True when a client object is ready.
    """
    global _PREPARED_CLIENT
    if _PREPARED_CLIENT is not None:
        return True
    try:
        env.pick_installation()
    except ComsolMcpError as exc:
        log.warning("No COMSOL installation found, JVM not started up front: %s", exc)
        return False
    try:
        import mph

        started = time.monotonic()
        _PREPARED_CLIENT = mph.Client(host=None)
        log.info("COMSOL client prepared in %.1fs (JVM started)", time.monotonic() - started)
        return True
    except Exception as exc:  # pragma: no cover - environment specific
        log.warning("Could not prepare the COMSOL client up front: %s", exc)
        return False


@dataclass
class MainModel:
    tag: str
    label: str
    path: str | None = None
    owner: str = "gui"  # "gui" when the COMSOL desktop opened it, "api" otherwise

    def as_dict(self) -> dict:
        return {"tag": self.tag, "label": self.label, "path": self.path, "owner": self.owner}


class Session:
    """Owns everything that talks to COMSOL inside one MCP server process."""

    def __init__(self, home: Path | None = None):
        self.home = Path(
            home or os.environ.get("COMSOL_MCP_HOME") or (Path.cwd() / ".comsol-mcp")
        )
        self.logs_dir = self.home / "logs"
        self.snapshots = SnapshotManager(self.home)

        self.install: env.Install | None = None
        self.port: int | None = None
        self.main: MainModel | None = None
        self.progress: ProgressStream | None = None
        self.progress_mode: str | None = None

        self._client: Any = None  # mph.Client, created at most once per process
        self.server_proc: subprocess.Popen | None = None
        self.server_log: Path | None = None
        self.gui_proc: subprocess.Popen | None = None
        self.gui_log: Path | None = None
        self.started_server = False
        self._log_handles: list[Any] = []
        self._last_snapshot = 0.0
        self._ghost_stop: threading.Event | None = None
        self._ghost_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ status

    @property
    def connected(self) -> bool:
        return self._client is not None and self.port is not None

    @property
    def client(self):
        self.require_connected()
        return self._client

    def require_connected(self) -> None:
        """Raise SessionError unless this session is connected to a server."""
        if not self.connected:
            raise SessionError(
                "Not connected to a COMSOL server.",
                hint="Call comsol_start_session (or comsol_attach_session) first.",
            )

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "client_version": getattr(self._client, "version", None) if self._client else None,
            "install": self.install.as_dict() if self.install else None,
            "main_model": self.main.as_dict() if self.main else None,
            "progress_mode": self.progress_mode,
            "server_pid": self._alive(self.server_proc),
            "gui_pid": self._alive(self.gui_proc),
            "gui_processes": self._gui_client_processes(),
            "home": str(self.home),
        }

    @staticmethod
    def _alive(proc: subprocess.Popen | None) -> int | None:
        if proc is None:
            return None
        return proc.pid if proc.poll() is None else None

    # ------------------------------------------------------------ connect/start

    def _ensure_client_object(self):
        """Return the MPh client object (once per process), without connecting.

        MPh refuses to instantiate more than one client per Python process, so
        this object is created with `host=None` (not stand-alone, not yet
        connected) and reused for every connect/disconnect cycle. Normally
        `prepare_client()` already created it in the main thread at startup;
        the fallback here only triggers when that was skipped.
        """
        if self._client is None:
            if _PREPARED_CLIENT is not None:
                self._client = _PREPARED_CLIENT
            else:
                import mph

                log.info("Starting the COMSOL client (JVM) ...")
                self._client = mph.Client(host=None)
        return self._client

    def _connect(self, port: int) -> None:
        client = self._ensure_client_object()
        try:
            client.connect(port)
        except Exception as exc:
            raise SessionError(
                f"Could not connect to the COMSOL server on port {port}: {exc}",
                hint="Check that the server is running and the port is free "
                     "(see .comsol-mcp/logs/ for server logs), then retry.",
            ) from exc
        self.port = port
        if self.install is None:
            self.install = self._install_for_version(getattr(client, "version", None))

    def disconnect(self, timeout: float = 15.0) -> None:
        """Disconnect from the server, bounded in time.

        `ModelUtil.disconnect()` is a server round-trip without a timeout; when
        a desktop client is disconnecting at the same moment it can block for a
        very long time (observed with two concurrent sessions). The real call
        runs on a daemon thread and we stop waiting after `timeout` seconds -
        the session counts as ended either way.
        """
        client = self._client
        if client is not None and client.port is not None:
            outcome: dict = {}

            def _do() -> None:
                try:
                    client.disconnect()
                    outcome["ok"] = True
                except Exception as exc:  # pragma: no cover - server hiccups
                    outcome["error"] = exc

            worker = threading.Thread(target=_do, daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                log.warning(
                    "Disconnect did not return within %.0f s; continuing anyway "
                    "(the COMSOL server keeps running until stopped).", timeout,
                )
            elif "error" in outcome:  # pragma: no cover
                log.warning("Disconnect failed: %s", outcome["error"])
            else:
                log.debug("Disconnected from the server")
        self.port = None
        self.progress_mode = None

    def _install_for_version(self, version: str | None) -> env.Install | None:
        try:
            return env.pick_installation(version)
        except ComsolMcpError:
            return None

    def _start_server(self, port: int) -> None:
        install = self.install
        if install is None:
            raise ComsolMcpError("No COMSOL installation selected.")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.server_log = self.logs_dir / f"mphserver-{port}.log"
        cmd = [str(install.server_exe), "-multi", "on", "-login", "auto", "-port", str(port)]
        log.info("Starting COMSOL server: %s", " ".join(cmd))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if env.IS_WINDOWS else 0
        handle = self.server_log.open("a", encoding="utf-8")
        self._log_handles.append(handle)
        self.server_proc = subprocess.Popen(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(self.logs_dir),
            creationflags=creationflags,
        )
        self.started_server = True
        if not env.wait_for_port(port, timeout=SERVER_WAIT_TIMEOUT):
            raise SessionError(
                f"The COMSOL server did not start listening on port {port} "
                f"within {SERVER_WAIT_TIMEOUT:.0f} s.",
                hint=f"Inspect {self.server_log} — a missing or busy license is the usual cause.",
            )

    def _launch_gui(self, model: Path | None) -> None:
        install = self.install
        if install is None or install.client_exe is None:
            raise ComsolMcpError(
                "The COMSOL desktop client launcher (comsolmphclient) was not found.",
                hint="Check the COMSOL installation, or run with gui=False to work headless.",
            )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.gui_log = self.logs_dir / "comsolmphclient.log"
        cmd = [str(install.client_exe), "-server", "localhost", "-port", str(self.port)]
        if model is not None:
            cmd += ["-open", str(model)]
        log.info("Opening the COMSOL desktop: %s", " ".join(cmd))
        handle = self.gui_log.open("a", encoding="utf-8")
        self._log_handles.append(handle)
        self.gui_proc = subprocess.Popen(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(self.home),
        )

    def _other_client_models(self) -> list[str]:
        """Model tags owned by other clients (in practice: the COMSOL desktop)."""
        try:
            return [str(tag) for tag in self.client.java.modelsUsedByOtherClients()]
        except Exception as exc:  # pragma: no cover
            log.debug("modelsUsedByOtherClients failed: %s", exc)
            return []

    def _yield_to_desktop(self) -> bool:
        """If the desktop shows its "服务器忙" modal, pause and return True.

        The dialog appears when the desktop's own server round-trip waits for
        another client - typically our polling during model load. Every query
        we send while it is up re-triggers it, and the user sees the dialog
        flash again and again ("the server keeps restarting"). So when it is
        present we stop querying for a few seconds and let the desktop finish.
        Cheap: one EnumWindows pass, no server round-trip.
        """
        if not desktop.busy_dialog_present(self.gui_pids()):
            return False
        time.sleep(3.0)
        return True

    def _wait_for_gui_model(self, model: Path | None) -> str:
        """Wait until the desktop reports a model, then return its tag."""
        deadline = time.monotonic() + GUI_WAIT_TIMEOUT
        wanted = model.name if model is not None else None
        seen: list[str] = []
        yielded = False
        while time.monotonic() < deadline:
            if self._yield_to_desktop():
                if not yielded:
                    log.info("Desktop shows its busy dialog; pausing server "
                             "queries until it finishes loading")
                    yielded = True
                continue
            tags = self._other_client_models()
            if tags and tags != seen:
                log.debug("Desktop reports models %s (wanted: %s)", tags, wanted)
                seen = list(tags)
            if wanted:
                for tag in tags:
                    try:
                        if str(self.client.java.model(tag).label()) == wanted:
                            return tag
                    except Exception:
                        continue
            if tags and wanted is None:
                return tags[0]
            time.sleep(5.0)
        raise SessionError(
            "The COMSOL desktop did not open a model in time.",
            hint="A dialog in the COMSOL window (login, license, model choice) usually blocks "
                 f"this. Models the desktop reported: {seen or 'none'}. Three things to check: "
                 "(1) dismiss any dialog in the COMSOL window; (2) if a COMSOL window from an "
                 "earlier session is still open, close it; (3) if a leftover '<model>.mph.lock' "
                 "file sits next to the model (from a previous session that was stopped while "
                 "the model was open), delete it and retry.",
        )

    def _model_state(self) -> str:
        """Rough readiness probe for the main model (see _wait_for_model_ready)."""
        try:
            java = self.client.java.model(self.main.tag)
            if not str(java.label()).strip():
                return "no label yet"
            components = [str(tag) for tag in java.component().tags()]
            for comp_tag in components[:3]:
                component = java.component(comp_tag)
                children = [
                    [str(t) for t in component.geom().tags()],
                    [str(t) for t in component.physics().tags()],
                    [str(t) for t in component.material().tags()],
                    [str(t) for t in component.mesh().tags()],
                ]
                if not any(children):
                    return f"component '{comp_tag}' still empty"
            return "ready"
        except Exception as exc:
            return f"error: {exc}"

    def _wait_for_model_ready(self) -> bool:
        """Wait until the desktop has finished loading the main model.

        While the desktop client is still deserializing a model, the server
        answers reads with degraded values (empty labels and child lists) and
        rejects other clients. Verified on COMSOL 6.2 (docs/findings-m0.md).
        """
        if self.main is None or self.main.owner != "gui":
            return True
        deadline = time.monotonic() + MODEL_READY_TIMEOUT
        state = "not checked"
        yielded = False
        while time.monotonic() < deadline:
            if self._yield_to_desktop():
                if not yielded:
                    log.info("Desktop busy dialog during model load; "
                             "pausing readiness polling")
                    yielded = True
                continue
            state = self._model_state()
            if state == "ready":
                log.debug("Main model is ready")
                return True
            time.sleep(5.0)
        log.warning(
            "Main model still looks incomplete after %.0f s (%s); continuing anyway",
            MODEL_READY_TIMEOUT, state,
        )
        return False

    def _attach_main(self, tag: str, owner: str, path: str | None = None) -> MainModel:
        java = self.client.java.model(tag)
        self.main = MainModel(
            tag=str(java.tag()),
            label=str(java.label()),
            path=path,
            owner=owner,
        )
        return self.main

    def _adopt_main_model(self, preferred: Path | None) -> None:
        """Pick the model the desktop already has open as our main model."""
        tags = self._other_client_models()
        if not tags:
            if preferred is not None:
                self._attach_main(self._load_via_api(preferred), owner="api", path=str(preferred))
            return
        target: str | None = None
        if preferred is not None:
            for tag in tags:
                try:
                    if str(self.client.java.model(tag).label()) == preferred.name:
                        target = tag
                        break
                except Exception:
                    continue
        target = target or tags[0]
        self._attach_main(target, owner="gui")

    def _load_via_api(self, model: Path) -> str:
        # client.load returns MPh's Model wrapper, not the Java object: the
        # tag lives on .java (Model itself has no `tag` attribute in MPh 1.4).
        loaded = self.client.load(str(model))
        return str(loaded.java.tag())

    def start(
        self,
        model_path: str | None = None,
        port: int = env.DEFAULT_PORT,
        gui: bool = True,
        version: str | None = None,
    ) -> dict:
        """Start (or reuse) the server, connect, open the model in the desktop."""
        if self.connected:
            if model_path:
                self.open_model(model_path)
            return self.status()

        self.install = env.pick_installation(version)
        if env.port_open(port):
            log.info("Reusing the COMSOL server already listening on port %s", port)
        else:
            self._start_server(port)
        self._connect(port)

        model: Path | None = None
        if model_path:
            model = Path(model_path).expanduser().resolve()
            if not model.exists():
                raise ModelError(
                    f"Model file not found: {model}",
                    hint="Check the path (absolute paths are safest) and retry.",
                )

        if gui:
            if self._other_client_models():
                # A desktop is already attached (leftover session, or the user
                # started one). Adopt its model instead of opening a second GUI.
                self._adopt_main_model(model)
            else:
                self._launch_gui(model)
                tag = self._wait_for_gui_model(model)
                self._attach_main(tag, owner="gui", path=str(model) if model else None)
        elif model is not None:
            self._attach_main(self._load_via_api(model), owner="api", path=str(model))
        else:
            self.main = None

        self._wait_for_model_ready()
        self.set_progress_mode(os.environ.get("COMSOL_MCP_PROGRESS", "stream"))
        if gui:
            self._start_ghost_sweeper()
        return self.status()

    def attach(self, port: int = env.DEFAULT_PORT, gui: bool = True) -> dict:
        """Attach to an already running COMSOL server."""
        if self.connected and self.port == port:
            return self.status()
        if self.connected:
            self.disconnect()
        self._connect(port)

        if self._other_client_models():
            self._adopt_main_model(None)
        elif gui:
            self._launch_gui(None)
            self._attach_main(self._wait_for_gui_model(None), owner="gui")
        self._wait_for_model_ready()
        self.set_progress_mode(os.environ.get("COMSOL_MCP_PROGRESS", "stream"))
        if gui:
            self._start_ghost_sweeper()
        return self.status()

    def open_model(self, model_path: str, display: bool = True) -> dict:
        """Make an existing file the main model of this session."""
        self.require_connected()
        target = Path(model_path).expanduser().resolve()
        if not target.exists():
            raise ModelError(f"Model file not found: {target}", hint="Check the path and retry.")

        for tag in self.client.java.tags():
            try:
                if str(self.client.java.model(tag).label()) == target.name:
                    owner = "gui" if str(tag) in self._other_client_models() else "api"
                    self._attach_main(str(tag), owner=owner)
                    return self.status()
            except Exception:
                continue

        self._attach_main(self._load_via_api(target), owner="api", path=str(target))
        if display and self._other_client_models():
            log.warning(
                "Model loaded through the API; the desktop window keeps showing its own model."
            )
        return self.status()

    # ------------------------------------------------------------------ models

    def list_models(self) -> list[dict]:
        used_by_others = set(self._other_client_models())
        models: list[dict] = []
        for tag in self.client.java.tags():
            java = self.client.java.model(tag)
            models.append(
                {
                    "tag": str(tag),
                    "label": str(java.label()),
                    "owner": "desktop" if str(tag) in used_by_others else "mcp",
                    "is_main": bool(self.main and self.main.tag == str(tag)),
                }
            )
        return models

    def main_model(self):
        """Return the main model as an `mph.Model`, verifying its identity."""
        import mph

        self.require_connected()
        if self.main is None:
            raise ModelError(
                "This session has no main model yet.",
                hint="Open a model with comsol_start_session(model_path=...) or comsol_load_model.",
            )
        tags = [str(tag) for tag in self.client.java.tags()]
        if self.main.tag not in tags:
            raise ModelError(
                f"The main model '{self.main.tag}' ({self.main.label}) is no longer loaded.",
                hint="Call comsol_list_models, then comsol_load_model to pick a model again.",
            )
        return mph.Model(self.client.java.model(self.main.tag))

    # ---------------------------------------------------------------- progress

    def set_progress_mode(self, mode: str) -> str:
        self.require_connected()
        mode = str(mode or "stream").lower()
        if mode not in PROGRESS_MODES:
            raise ComsolMcpError(
                f"Unknown progress mode '{mode}'.",
                hint=f"Use one of: {', '.join(PROGRESS_MODES)}.",
            )
        if mode == "stream":
            if self.progress is None:
                self.progress = ProgressStream(self.logs_dir / f"progress-{self.port}.log")
            # showProgress(False) first: it also closes COMSOL's own progress
            # window, which otherwise stays behind as an empty floating box
            # after desktop-mode operations finish (reported live as a
            # mysterious blank area over the model tree).
            self._client.java.showProgress(False)
            self.progress.enable(self._client)
        elif mode == "desktop":
            self._client.java.showProgress(True)
        else:
            self._client.java.showProgress(False)
        self.progress_mode = mode
        return mode

    def poll_progress(self) -> tuple[dict | None, list[str]]:
        """Return (parsed state, new lines) since the last poll, or (None, [])."""
        if self.progress is None:
            return None, []
        lines = self.progress.read_new()
        if not lines:
            return None, []
        return self.progress.parse(lines), lines

    # ------------------------------------------------------- snapshots / saving

    def snapshot(self, label: str | None = None) -> Path:
        model = self.main_model()
        return self.snapshots.snapshot(model, label or (self.main.label if self.main else None))

    def autosnapshot(self, label: str, min_interval: float = 60.0) -> Path | None:
        """Snapshot before a write operation, throttled so edit bursts don't spam files.

        Disable with COMSOL_MCP_AUTOSNAPSHOT=0 (for very large models).
        """
        disabled = os.environ.get("COMSOL_MCP_AUTOSNAPSHOT", "1").strip().lower()
        if disabled in ("0", "false", "no", "off"):
            return None
        now = time.monotonic()
        if now - self._last_snapshot < min_interval:
            return None
        try:
            path = self.snapshot(label)
        except Exception as exc:
            log.warning("Automatic snapshot failed: %s", exc)
            return None
        self._last_snapshot = now
        self.snapshots.prune(keep=60)
        return path

    def save(self, path: str | None = None, allow_overwrite: bool = False) -> Path:
        """Save the main model. Never overwrites an existing file unless asked."""
        model = self.main_model()
        if path:
            target = Path(path).expanduser().resolve()
            if target.exists() and not allow_overwrite:
                raise ModelError(
                    f"Refusing to overwrite the existing file {target}.",
                    hint="Pass a different path, or allow_overwrite=True once the user confirmed.",
                )
        else:
            folder = self.home / "saved"
            folder.mkdir(parents=True, exist_ok=True)
            stem = Path(self.main.label).stem if self.main else "model"
            target = folder / f"{stem}.mph"
            if target.exists():
                target = folder / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.mph"
        save_copy(model, str(target))
        return target

    # ------------------------------------------------------------------- ending

    def gui_pids(self) -> list[int]:
        """Process ids of COMSOL desktop clients: ours first, then any others."""
        pids: list[int] = []
        if self.gui_proc is not None and self.gui_proc.poll() is None:
            pids.append(self.gui_proc.pid)
        for pid in self._gui_client_processes():
            if pid not in pids:
                pids.append(pid)
        return pids

    # ------------------------------------------------------- ghost sweeper

    def _start_ghost_sweeper(self) -> None:
        """Keep the desktop free of IME ghost windows while the session lives.

        The Chinese-IME `CiceroUIWndFrame` re-appears every time the COMSOL
        window is activated, so a sweep at workflow checkpoints leaves it
        visible in between (users see it flash over the model tree). This
        daemon hides it every couple of seconds - only the known-bad floating
        classes, never panels, never while progress is fresh, never a kill.
        """
        if self._ghost_thread is not None and self._ghost_thread.is_alive():
            return
        stop = self._ghost_stop = threading.Event()

        def loop() -> None:
            while not stop.wait(2.0):
                try:
                    pids = self.gui_pids()
                    if not pids:
                        return          # desktop gone; nothing left to sweep
                    progress_log = (self.progress.path
                                    if self.progress is not None else None)
                    desktop.hide_stale_windows(
                        pids, progress_log,
                        live_progress=(self.progress_mode == "desktop"))
                except Exception as exc:  # pragma: no cover - best effort
                    log.debug("ghost sweep failed: %s", exc)

        self._ghost_thread = threading.Thread(
            target=loop, daemon=True, name="comsol-ghost-sweeper")
        self._ghost_thread.start()

    def _stop_ghost_sweeper(self) -> None:
        if self._ghost_stop is not None:
            self._ghost_stop.set()
        self._ghost_stop = None
        self._ghost_thread = None

    def focus_desktop(self) -> dict:
        """Bring the COMSOL desktop window to the front (best effort)."""
        report = desktop.focus(self.gui_pids())
        progress_log = (self.progress.path
                        if self.progress is not None else None)
        stale = desktop.hide_stale_windows(
            self.gui_pids(), progress_log,
            live_progress=(self.progress_mode == "desktop"))
        if stale:
            report["stale_windows_hidden"] = stale
        return report

    def close_desktop(self, dismiss_save_dialog: bool = True) -> dict:
        """Close the COMSOL desktop window gracefully (handles a save dialog)."""
        report = desktop.close(self.gui_pids(), dismiss_save_dialog=dismiss_save_dialog)
        if report.get("closed"):
            self.gui_proc = None
        return report

    def create_model(self, name: str | None = None) -> MainModel:
        """Create an empty model on the server and make it the main model.

        The desktop only displays models it opened itself, so a model created
        this way is not shown in the COMSOL window (see comsol_create_model).
        """
        self.require_connected()
        try:
            created = self._client.create(name)
        except Exception as exc:
            raise ComsolMcpError(
                f"Could not create a model: {exc}",
                hint="Call comsol_list_models to see what is already on the server.",
            ) from exc
        return self._attach_main(str(created.java.tag()), owner="api")

    def end(self, save: bool = False, stop_server: bool = False,
            close_gui: bool = False) -> dict:
        report: dict[str, Any] = {"snapshot": None, "server_stopped": False, "notes": []}
        self._stop_ghost_sweeper()
        # Order matters (all verified live):
        # 1. disconnect while the server is idle - once the desktop window
        #    starts closing it holds the server, and our disconnect can then
        #    block past any timeout;
        # 2. close the desktop window, so no stale <model>.mph.lock survives;
        # 3. only then stop the server. Killing it while the JVM client is
        #    still attached crashes the JVM (and this MCP process with it).
        if self.connected:
            if save and self.main is not None:
                try:
                    report["snapshot"] = str(self.snapshot("session-end"))
                except Exception as exc:
                    report["notes"].append(f"Snapshot failed: {exc}")
            self.disconnect(timeout=15)
        if close_gui:
            report["desktop"] = self.close_desktop()
        # Never stop the server while the desktop still holds it: an orphaned
        # desktop floods itself with "connection lost / save changes / quit"
        # modals (observed live - the user reads it as the server restarting
        # over and over), and answering them is not reliable. If the graceful
        # close did not finish, keep the server alive and hand the window back
        # to the user to close from its UI.
        desktop_still_open = bool(report.get("desktop", {}).get("still_open")) \
            if close_gui else bool(self._gui_client_processes())
        if self.connected and stop_server:
            report["notes"].append(
                "Could not disconnect in time; the server was left running to "
                "avoid crashing the JVM client. Stop it manually."
            )
        if stop_server and self.started_server and self.server_proc is not None:
            if desktop_still_open:
                report["server_stopped"] = False
                report["notes"].append(
                    "The server was left running because the COMSOL desktop "
                    "window did not close: stopping it now would orphan the "
                    "window and trigger a flood of 'connection lost / save "
                    "changes' dialogs. Close the COMSOL window from its UI, "
                    "then the server can be stopped."
                )
            elif self.server_proc.poll() is None:
                self.server_proc.terminate()
                try:
                    self.server_proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.server_proc.kill()
                report["server_stopped"] = True
                self.started_server = False
            else:
                report["server_stopped"] = True
                self.started_server = False
        for handle in self._log_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._log_handles.clear()
        if self._gui_client_processes():
            report["notes"].append(
                "A COMSOL desktop client window is still open. Close it from the COMSOL UI when "
                "you are done (it may ask to save). Never kill it with taskkill: that corrupts the "
                "server-side desktop session, and later connections fail until the server restarts."
            )
        return report

    # ---------------------------------------------------------------- processes

    @staticmethod
    def _gui_client_processes() -> list[int]:
        """PIDs of running COMSOL desktop clients (best effort per platform)."""
        try:
            if env.IS_WINDOWS:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq comsolmphclient.exe", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout
                pids: list[int] = []
                for line in out.splitlines():
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 2 and parts[0].lower().startswith("comsolmphclient"):
                        try:
                            pids.append(int(parts[1]))
                        except ValueError:
                            continue
                return pids
            out = subprocess.run(
                ["pgrep", "-f", "comsolmphclient"], capture_output=True, text=True, timeout=15
            ).stdout
            return [int(line) for line in out.split() if line.strip().isdigit()]
        except Exception:
            return []

    def detect(self) -> dict:
        """Environment report for comsol_detect: installs, processes, ports."""
        log.debug("detect: entry")
        installs = env.find_installations()
        log.debug("detect: installs done")
        ports: dict[str, bool] = {}
        for candidate in sorted({env.DEFAULT_PORT, self.port or env.DEFAULT_PORT}):
            ports[str(candidate)] = env.port_open(candidate)
        log.debug("detect: probing ports")
        return {
            "installs": [install.as_dict() for install in installs],
            "default_port": env.DEFAULT_PORT,
            "ports_listening": {k: v for k, v in ports.items() if v},
            "gui_processes": self._gui_client_processes(),
            "session": self.status(),
        }
