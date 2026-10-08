"""Unit tests for progress-log parsing (COMSOL 6.2 text formats)."""

from __future__ import annotations

from pathlib import Path

from comsol_mcp.progress import ProgressStream


def test_parse_chinese_progress_section():
    lines = [
        '<---- Study 1/Solution 1 (sol1) 中的"稳态求解器 1" ----',
        "线性求解器 ",
        "求解的自由度数：111971。",
        "---        当前进度:  37 % - 约束处理",
        "Memory: 1313/1327 1394/1414",
    ]
    state = ProgressStream.parse(lines)
    assert state["percent"] == 37
    assert state["stage"] == "约束处理"
    assert state["section"].startswith("Study 1/Solution 1")
    assert state["memory"] == "1313/1327"
    assert ProgressStream.describe(state)


def test_parse_english_progress():
    lines = [
        "<---- Stationary Solver 1 in Study 1/Solution 1 ----",
        "---  Current progress:  100 % - Solving linear system",
    ]
    state = ProgressStream.parse(lines)
    assert state["percent"] == 100
    assert state["stage"] == "Solving linear system"


def test_read_new_returns_only_new_lines(tmp_path: Path):
    path = tmp_path / "progress.log"
    stream = ProgressStream(path)
    stream.enable = lambda client: None  # not used here

    path.write_text("line one\n", encoding="utf-8")
    assert stream.read_new() == ["line one"]

    with path.open("a", encoding="utf-8") as handle:
        handle.write("line two\npartial")
    assert stream.read_new() == ["line two"]   # keeps the partial tail buffered

    with path.open("a", encoding="utf-8") as handle:
        handle.write(" line\n")
    assert stream.read_new() == ["partial line"]


def test_read_new_decodes_gbk_and_tracks_bytes(tmp_path: Path):
    """A GBK log (Chinese Windows) must decode, and a multi-byte tail split
    across reads must not desync the byte cursor."""
    path = tmp_path / "progress.log"
    stream = ProgressStream(path)
    line = "---        当前进度:  37 % - 约束处理"

    with path.open("wb") as handle:
        handle.write(line.encode("gbk") + b"\n")
    assert stream.read_new() == [line]

    # Write half a multi-byte character, read (nothing complete), then finish.
    encoded = line.encode("gbk")
    with path.open("ab") as handle:
        handle.write(encoded[: len(encoded) // 2])
    assert stream.read_new() == []          # no newline yet -> buffered
    with path.open("ab") as handle:
        handle.write(encoded[len(encoded) // 2:] + b"\n")
    assert stream.read_new() == [line]


def test_missing_file_is_not_an_error(tmp_path: Path):
    stream = ProgressStream(tmp_path / "nope.log")
    assert stream.read_new() == []
    assert stream.parse([]) == {"percent": None, "stage": None, "section": None,
                                "memory": None, "last_line": None}
