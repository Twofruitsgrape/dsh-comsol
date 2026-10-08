"""Platform helpers to bring the COMSOL desktop window forward and close it.

Both operations are best effort and **never force-kill**: a force-killed
desktop corrupts the server-side session (see docs/findings-m0.md). Closing
gracefully is also what avoids the stale `<model>.mph.lock` trap, because the
lock only survives when a server is stopped while the window is still open.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_DIALOG_SCRIPT = Path(__file__).with_name("scripts") / "close_desktop_dialog.ps1"

# Buttons that mean "close without saving". COMSOL 6.2 shows English labels
# even in a Chinese UI (verified), but the localized variants are listed too.
# The clicker matches normalized prefixes, so "No" also hits "否(N)".
DISMISS_BUTTONS = ("No", "Don't Save", "不保存", "否")

# The desktop raises this modal whenever *its* server round-trip has to wait
# for another client (verified 6.2 Chinese UI: caption "服务器忙", body
# "服务器已被其他客户端使用", Quit button + green progress bar). While it is
# up, our API client must stop polling the server (see
# Session._wait_for_gui_model / _wait_for_model_ready): every extra
# round-trip we make re-triggers the dialog, which users read as the server
# "restarting" over and over.
BUSY_DIALOG_MARKERS = ("服务器忙", "等待服务空闲", "服务空闲",
                       "server busy", "waiting for the server",
                       "server to become idle")


def busy_dialog_present(pids: list[int]) -> bool:
    """True while the desktop shows its "waiting for the server" modal.

    Cheap: one EnumWindows pass over the desktop processes, no server
    round-trip (a round-trip is exactly what we are trying to avoid).
    """
    if not IS_WINDOWS or not pids:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    for pid in pids:
        for info in (_window_info(user32, hwnd)
                     for hwnd in _all_window_handles(pid)):
            if not info or not info["visible"]:
                continue
            title = str(info.get("title", "")).lower()
            if any(marker.lower() in title for marker in BUSY_DIALOG_MARKERS):
                return True
    return False

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW if IS_WINDOWS else 0,
        )
    except Exception as exc:
        log.debug("Command %s failed: %s", cmd, exc)
        return None


def pids_alive(pids: list[int]) -> list[int]:
    """Which of the given process ids are still running."""
    alive: list[int] = []
    for pid in pids:
        if IS_WINDOWS:
            result = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=15)
            text = (result.stdout if result else "") or ""
            if f'"{pid}"' in text:
                alive.append(pid)
        else:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                continue
    return alive


def _window_handles(pid: int) -> list[int]:
    """Top-level window handles owned by `pid` (Windows only)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.GetWindowTextLengthW(hwnd):
            handles.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("EnumWindows failed: %s", exc)
    return handles


def _all_window_handles(pid: int) -> list[int]:
    """All top-level window handles owned by `pid`, titled or not (Windows only)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            handles.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("EnumWindows failed: %s", exc)
    return handles


def _window_info(user32, hwnd: int) -> dict | None:
    """Class name, title length, visibility and rect of one window."""
    import ctypes
    from ctypes import wintypes

    try:
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        title_len = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if title_len and title_len < 512:
            buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buf, title_len + 1)
            title = buf.value
        visible = bool(user32.IsWindowVisible(hwnd))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        try:
            style = user32.GetWindowLongW(hwnd, _GWL_STYLE) & 0xFFFFFFFF
        except Exception:
            style = 0
        return {
            "hwnd": hwnd,
            "class": class_name.value,
            "title": title,
            "title_len": int(title_len),
            "visible": visible,
            "area": width * height,
            "popup": not style & _WS_CHILD,
        }
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("Could not inspect window %s: %s", hwnd, exc)
        return None


# Window classes COMSOL's own transient dialogs use on Windows. The main
# desktop window is excluded by size before this list ever matters.
_DIALOG_CLASSES = frozenset({"#32770", "Qt5QWindowIcon", "Qt5QWindowToolSaveBits"})

# Child windows are the application's own panels (model tree, settings,
# graphics) — never hide an unknown one. Only these known-bad floating
# classes are hidden anywhere in the window tree.
_BAD_CHILD_CLASSES = frozenset({
    "CiceroUIWndFrame", "tooltips_class32", "MSCTFIME UI",
})

# Win32 style bits used to tell a floating window from a docked panel.
# (Top-level windows carry WS_OVERLAPPED == 0, so only WS_CHILD marks a panel.)
_WS_CHILD = 0x40000000
_GWL_STYLE = -16
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_OFFSCREEN = -32000

# A visible window is only a hiding candidate while it is small next to the
# main COMSOL window — the main window is always the largest by far.
_STALE_AREA_RATIO = 0.15

# While COMSOL is actively logging progress a small dialog may be the live
# progress window itself: never hide anything then.
_PROGRESS_FRESH_SECONDS = 15.0


def _progress_log_fresh(progress_log) -> bool:
    """True when COMSOL wrote progress recently — a live window may be up."""
    if not progress_log:
        return False
    try:
        age = time.time() - Path(progress_log).stat().st_mtime
        return age < _PROGRESS_FRESH_SECONDS
    except OSError:
        return False


def _descendant_infos(user32, parent: int) -> list[dict]:
    """Every visible descendant of `parent`, at any depth (Windows only).

    The blank box over the model tree is not a direct child of the main
    window — Java/AWT nests its popups and heavyweight frames several
    levels deep, so a one-level sweep misses it.
    """
    import ctypes
    from ctypes import wintypes

    found: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        info = _window_info(user32, int(hwnd))
        if info and info["visible"]:
            found.append(info)
        return True

    try:
        user32.EnumChildWindows(parent, callback, 0)
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("EnumChildWindows failed for %s: %s", parent, exc)
    return found


def _should_hide(info: dict, main_area: int, progress_fresh: bool) -> bool:
    """Pure predicate: is this visible window safe-to-hide residue?

    Docked panels (WS_CHILD: model tree, settings, graphics) are never
    touched. Only small *floating* windows qualify: known-bad IME/tooltip
    classes, or untitled popup frames (COMSOL's progress dialog surviving a
    progress-mode switch, blank Java popups). A titled progress-like dialog
    is only hidden when no progress was logged recently.
    """
    if not info.get("visible"):
        return False
    area = info.get("area", 0)
    if not area or not main_area or area >= main_area * _STALE_AREA_RATIO:
        return False
    cls = str(info.get("class", ""))
    if cls in _BAD_CHILD_CLASSES:
        return True
    if not info.get("popup"):
        return False                      # a docked panel of the main window
    if not info.get("title_len"):
        return True                       # untitled floating residue
    if progress_fresh:
        return False                       # a live progress window may be up
    if cls in _DIALOG_CLASSES and ("rogress" in str(info.get("title", ""))
                                   or "进度" in str(info.get("title", ""))):
        return True                       # leftover COMSOL progress dialog
    return False


def hide_stale_windows(pids: list[int], progress_log=None,
                       live_progress: bool = False) -> int:
    """Hide small stale floating windows of the given processes (Windows only).

    Covers the blank box users see over the model tree: COMSOL's own progress
    dialog occasionally survives a progress-mode switch as an empty floating
    frame, and the Chinese IME leaves `CiceroUIWndFrame` ghosts behind. Both
    are untitled, small next to the main window, and safe to hide.

    `live_progress=True` (desktop progress mode) protects every titled dialog
    unconditionally: the progress file is never written in that mode, so the
    freshness check alone would hide COMSOL's live progress window mid-solve.

    Never hides the main window or any docked panel, never touches anything
    while COMSOL is actively logging progress, and never kills a process.
    Returns the number of windows hidden.
    """
    if not IS_WINDOWS or not pids:
        return 0
    fresh = live_progress or _progress_log_fresh(progress_log)
    import ctypes

    user32 = ctypes.windll.user32
    hidden = 0
    for pid in pids:
        top = [_window_info(user32, hwnd) for hwnd in _all_window_handles(pid)]
        infos = [info for info in top if info and info["visible"]]
        if not infos:
            continue
        main_area = max(info["area"] for info in infos)
        if main_area <= 0:
            continue
        main_hwnd = max(infos, key=lambda info: info["area"])["hwnd"]
        candidates = list(infos) + _descendant_infos(user32, main_hwnd)
        for info in candidates:
            if info["area"] >= main_area:
                continue  # the main window itself
            if _should_hide(info, main_area, fresh):
                try:
                    user32.ShowWindow(info["hwnd"], 0)  # SW_HIDE
                    hidden += 1
                    log.info("Hid stale %s window (hwnd=%s area=%s title=%r)",
                             info["class"] or "<no class>", info["hwnd"],
                             info["area"], info.get("title", ""))
                except Exception as exc:  # pragma: no cover - platform specific
                    log.debug("Could not hide window %s: %s",
                              info["hwnd"], exc)
    return hidden


def hide_ime_ghosts(pids: list[int]) -> int:
    """Hide blank 'CiceroUIWndFrame' ghost windows of the given processes.

    With a Chinese IME active, Windows' text-services frame sometimes gets
    stuck visible after foreground switching and mouse automation: a blank
    white box (class CiceroUIWndFrame) floating over the application. It
    belongs to the focused process, renders nothing and is safe to hide.
    Returns the number of windows hidden.
    """
    if not IS_WINDOWS:
        return 0
    return hide_stale_windows(pids)


def focus(pids: list[int]) -> dict:
    """Bring the first COMSOL desktop window of `pids` to the foreground."""
    if not pids:
        return {"focused": False, "note": "No COMSOL desktop client process is running."}
    if not IS_WINDOWS:
        return {
            "focused": False,
            "note": "Bringing the COMSOL window to the front is only implemented on Windows.",
        }
    import ctypes

    user32 = ctypes.windll.user32
    for pid in pids:
        for hwnd in _window_handles(pid):
            try:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.4)
                return {"focused": True, "pid": pid, "hwnd": hwnd}
            except Exception as exc:
                log.debug("Could not focus window %s: %s", hwnd, exc)
    return {"focused": False, "note": "No visible COMSOL window was found for the running client."}


def click_dialog_button(pids: list[int], names: tuple[str, ...] = DISMISS_BUTTONS) -> str | None:
    """Click a matching button in a COMSOL dialog. Returns the button name clicked."""
    if not IS_WINDOWS or not _DIALOG_SCRIPT.exists():
        return None
    result = _run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(_DIALOG_SCRIPT),
            "-Names", ",".join(names),
            "-Pids", ",".join(str(pid) for pid in pids),
        ],
        timeout=30,
    )
    if result is None:
        return None
    for line in (result.stdout or "").splitlines():
        if line.startswith("CLICKED:"):
            return line.split(":", 1)[1].strip()
    log.info("Dialog click helper found no button (rc=%s): %s",
             result.returncode, (result.stdout or result.stderr or "").strip()[:800])
    return None


# Dialog captions that must never receive the Enter fallback: a
# save-changes prompt may default to "Yes" and Enter would silently accept it.
_SAVE_CAPTION_MARKERS = ("保存", "save", "更改", "changes")


def press_dialog_default(pids: list[int]) -> bool:
    """Press Enter on a small titled COMSOL dialog (Windows only).

    Java **AWT** dialogs - e.g. the "等待服务空闲 / waiting for the server to
    become idle" frame with its green progress bar - expose no buttons to UI
    Automation at all, so the click helper can never match them. Their default
    button (Quit / 退出) is the safe dismiss action, and Enter reaches it
    regardless of the toolkit. Only untitled-exempt, small, *titled* dialogs
    of the given processes are targeted; the main window is never touched, and
    save-changes prompts are skipped so Enter can never accept one.
    """
    if not IS_WINDOWS or not pids:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    pressed = False
    for pid in pids:
        infos = [info for info in
                 (_window_info(user32, hwnd) for hwnd in _all_window_handles(pid))
                 if info and info["visible"]]
        if not infos:
            continue
        main_area = max(info["area"] for info in infos)
        for info in infos:
            if info["area"] >= main_area * _STALE_AREA_RATIO:
                continue           # the main window
            if not info.get("title_len"):
                continue           # untitled ghosts are hidden, not pressed
            title = str(info.get("title", "")).lower()
            if any(marker in title for marker in _SAVE_CAPTION_MARKERS):
                continue           # never Enter on a save-changes prompt
            if any(marker.lower() in title for marker in BUSY_DIALOG_MARKERS):
                # Enter would press Quit there, which CANCELS the pending
                # close operation the dialog is only waiting for.
                continue
            try:
                user32.SetForegroundWindow(info["hwnd"])
                time.sleep(0.3)
                # VK_RETURN down/up as real hardware events
                user32.keybd_event(0x0D, 0, 0, 0)
                time.sleep(0.05)
                user32.keybd_event(0x0D, 0, 2, 0)
                log.info("Pressed Enter on COMSOL dialog %r (hwnd=%s)",
                         info["title"], info["hwnd"])
                pressed = True
            except Exception as exc:  # pragma: no cover - platform specific
                log.debug("Enter to dialog %s failed: %s", info["hwnd"], exc)
    return pressed


def close(pids: list[int], dismiss_save_dialog: bool = True, timeout: float = 45.0) -> dict:
    """Ask the COMSOL window(s) to close, dismissing a save-changes dialog if needed."""
    report: dict = {
        "pids": list(pids),
        "closed": [],
        "still_open": [],
        "dialogs_dismissed": [],
        "notes": [],
    }
    if not pids:
        report["notes"].append("No COMSOL desktop client process was running.")
        return report

    for pid in pids:
        _run(["taskkill", "/PID", str(pid)])  # WM_CLOSE: graceful, no /F

    deadline = time.monotonic() + timeout
    first_round = True
    while time.monotonic() < deadline:
        if not pids_alive(pids):
            break
        # Give the dialog a moment to appear, then make sure the COMSOL window
        # is on top: the click is a real mouse click at screen coordinates and
        # would otherwise land on whatever window covers that spot.
        time.sleep(3.0 if first_round else 1.5)
        first_round = False
        if dismiss_save_dialog:
            focus(pids)
            # raising the window re-shows the IME ghost; sweep it away before
            # the click so it cannot cover the dialog buttons.
            hide_stale_windows(pids)
            time.sleep(0.6)
            # Answer the save-changes prompt with No / 否. Do NOT touch the
            # "服务器忙 / waiting for the server" modal: that is the desktop's
            # own wait dialog for its pending close operation - clicking its
            # Quit button cancels the very close we are waiting for (observed
            # live: five Quit clicks, the window never exited). It resolves by
            # itself while the server stays alive.
            clicked = click_dialog_button(pids)
            if clicked:
                report["dialogs_dismissed"].append(clicked)
            else:
                press_dialog_default(pids)

    # one last sweep: the close handshake itself can leave ghosts behind
    hide_stale_windows(pids)

    # COMSOL exits with an animation after its last dialog closes; give the
    # process a short grace period before declaring it still_open, otherwise
    # the report races its own shutdown and strands <model>.mph.lock. Only
    # when we actually answered a dialog (the window was closing); a window
    # that never showed one is genuinely stuck and not worth waiting on.
    if report["dialogs_dismissed"]:
        grace_deadline = time.monotonic() + 10.0
        while time.monotonic() < grace_deadline and pids_alive(pids):
            time.sleep(1.0)

    alive = pids_alive(pids)
    report["closed"] = [pid for pid in pids if pid not in alive]
    report["still_open"] = alive
    if alive:
        report["notes"].append(
            "The COMSOL window did not close (a dialog may need attention). Close it from the "
            "COMSOL UI - never force-kill it while a server holds the model."
        )
    elif report["dialogs_dismissed"]:
        report["notes"].append(
            "COMSOL asked to save changes; answered No. The MCP keeps snapshots under "
            ".comsol-mcp/history/, so nothing is lost."
        )
    return report
