"""Unit tests for the desktop window helpers (no COMSOL, no real window)."""

from __future__ import annotations

import comsol_mcp.desktop as desktop


def test_helpers_are_graceful_without_processes():
    assert desktop.pids_alive([]) == []
    report = desktop.close([])
    assert report["closed"] == [] and report["still_open"] == []
    assert report["notes"], "closing nothing should explain itself"
    focused = desktop.focus([])
    assert focused["focused"] is False and focused["note"]


def test_close_reports_pids_it_could_not_close(monkeypatch):
    # pretend a window that never goes away
    monkeypatch.setattr(desktop, "pids_alive", lambda pids: list(pids))
    monkeypatch.setattr(desktop, "_run", lambda *a, **k: None)
    report = desktop.close([4242], dismiss_save_dialog=False, timeout=0.01)
    assert report["still_open"] == [4242]
    assert any("never force-kill" in note for note in report["notes"])


def test_click_dialog_button_is_noop_without_script(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_DIALOG_SCRIPT", tmp_path / "missing.ps1")
    assert desktop.click_dialog_button([1]) is None


def test_press_dialog_default_is_noop_without_processes():
    assert desktop.press_dialog_default([]) is False


def test_busy_dialog_present_matches_captions(monkeypatch):
    monkeypatch.setattr(desktop, "_all_window_handles", lambda pid: [11, 12])
    windows = {
        11: {"visible": True, "title": "busbar.mph - COMSOL Multiphysics",
             "area": 1_000_000},
        12: {"visible": True, "title": "服务器忙", "area": 400},
    }
    monkeypatch.setattr(desktop, "_window_info", lambda u, h: windows[h])
    assert desktop.busy_dialog_present([4242]) is True
    windows[12]["title"] = "some other dialog"
    assert desktop.busy_dialog_present([4242]) is False
    windows[12]["title"] = "Server Busy"
    windows[12]["visible"] = False
    assert desktop.busy_dialog_present([4242]) is False   # hidden: not up


def _win(area, title_len=0, visible=True, popup=True, cls="", title=""):
    return {"visible": visible, "area": area, "title_len": title_len,
            "popup": popup, "class": cls, "title": title}


def test_should_hide_needs_small_visible_window():
    main = 1_000_000
    assert desktop._should_hide(_win(main // 20), main, False)
    assert not desktop._should_hide(_win(main // 2), main, False)
    assert not desktop._should_hide(_win(main // 20, visible=False), main, False)


def test_should_hide_titled_progress_dialog_only_when_log_quiet():
    main = 1_000_000
    progress = _win(main // 20, title_len=2, cls="#32770", title="进度")
    assert desktop._should_hide(progress, main, False)
    assert not desktop._should_hide(progress, main, True)
    # a titled non-progress dialog is never hidden
    assert not desktop._should_hide(
        _win(main // 20, title_len=19, cls="#32770",
             title="COMSOL Multiphysics"), main, False)


def test_should_hide_never_touches_docked_panels():
    """A small untitled WS_CHILD panel (model tree etc.) must survive."""
    main = 1_000_000
    assert not desktop._should_hide(_win(main // 20, popup=False), main, False)


def test_should_hide_known_bad_classes_even_when_docked():
    main = 1_000_000
    for cls in desktop._BAD_CHILD_CLASSES:
        assert desktop._should_hide(
            _win(main // 20, popup=False, cls=cls), main, True)


def test_hide_stale_windows_protects_live_desktop_progress(monkeypatch):
    """Desktop progress mode never writes the progress file, so the freshness
    check is useless; live_progress=True must protect the titled dialog."""
    infos = {
        11: {"hwnd": 11, "class": "SunAwtFrame", "title": "COMSOL",
             "title_len": 6, "visible": True, "area": 1_000_000,
             "popup": True},
        12: {"hwnd": 12, "class": "#32770", "title": "进度", "title_len": 2,
             "visible": True, "area": 10_000, "popup": True},
    }
    monkeypatch.setattr(desktop, "_all_window_handles", lambda pid: [11, 12])
    monkeypatch.setattr(desktop, "_window_info", lambda u, h: infos[h])
    monkeypatch.setattr(desktop, "_descendant_infos", lambda u, p: [])
    monkeypatch.setattr(desktop, "_progress_log_fresh", lambda p: False)

    class User32:
        def ShowWindow(self, hwnd, cmd):
            raise AssertionError("must not hide the live progress window")

    import ctypes
    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"user32": User32()})(), raising=False)
    assert desktop.hide_stale_windows([4242], None, live_progress=True) == 0


def test_hide_stale_windows_skips_fresh_progress(monkeypatch, tmp_path):
    log = tmp_path / "progress-2036.log"
    log.write_text("x", encoding="utf-8")
    monkeypatch.setattr(desktop, "_all_window_handles", lambda pid: [11])
    monkeypatch.setattr(desktop, "_descendant_infos", lambda u, p: [])
    monkeypatch.setattr(
        desktop, "_window_info",
        lambda u, h: {"hwnd": h, "class": "#32770", "title": "进度",
                      "title_len": 2, "visible": True, "area": 100,
                      "popup": True})
    monkeypatch.setattr(desktop, "_progress_log_fresh", lambda p: True)
    # fresh log -> titled progress dialog must survive (no ShowWindow call)
    import ctypes

    class NoHide:
        def ShowWindow(self, hwnd, cmd):  # pragma: no cover - must not run
            raise AssertionError("must not hide while progress is fresh")

    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"user32": NoHide()})(), raising=False)
    assert desktop.hide_stale_windows([4242], log) == 0


def test_hide_stale_windows_hides_untitled_when_busy(monkeypatch):
    seen = []
    infos = {
        11: {"hwnd": 11, "class": "COMSOL", "title": "main",
             "title_len": 4, "visible": True, "area": 1_000_000,
             "popup": True},
        12: {"hwnd": 12, "class": "Qt5QWindowIcon", "title": "",
             "title_len": 0, "visible": True, "area": 10_000,
             "popup": True},
    }
    monkeypatch.setattr(desktop, "_all_window_handles", lambda pid: [11, 12])
    monkeypatch.setattr(desktop, "_window_info", lambda u, h: infos[h])
    monkeypatch.setattr(desktop, "_descendant_infos", lambda u, p: [])
    monkeypatch.setattr(desktop, "_progress_log_fresh", lambda p: True)

    class User32:
        def ShowWindow(self, hwnd, cmd):
            seen.append(hwnd)
            return True

    import ctypes
    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"user32": User32()})(), raising=False)
    assert desktop.hide_stale_windows([4242], None) == 1
    assert seen == [12]


def test_hide_stale_windows_reaches_nested_ghosts(monkeypatch):
    """A blank IME ghost several levels below the main window is hidden,
    while its docked siblings survive."""
    seen = []
    main = {"hwnd": 11, "class": "SunAwtFrame", "title": "COMSOL",
            "title_len": 6, "visible": True, "area": 1_000_000,
            "popup": True}
    descendants = [
        {"hwnd": 21, "class": "SunAwtCanvas", "title": "", "title_len": 0,
         "visible": True, "area": 200_000, "popup": False},   # panel: keep
        {"hwnd": 22, "class": "CiceroUIWndFrame", "title": "", "title_len": 0,
         "visible": True, "area": 9_000, "popup": False},     # ghost: hide
    ]
    monkeypatch.setattr(desktop, "_all_window_handles", lambda pid: [11])
    monkeypatch.setattr(desktop, "_window_info", lambda u, h: main)
    monkeypatch.setattr(desktop, "_descendant_infos", lambda u, p: descendants)
    monkeypatch.setattr(desktop, "_progress_log_fresh", lambda p: False)

    class User32:
        def ShowWindow(self, hwnd, cmd):
            seen.append(hwnd)
            return True

    import ctypes
    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"user32": User32()})(), raising=False)
    assert desktop.hide_stale_windows([4242], None) == 1
    assert seen == [22]


def test_hide_ime_ghosts_keeps_old_name():
    assert desktop.hide_stale_windows([]) == 0
    assert desktop.hide_ime_ghosts([]) == 0
