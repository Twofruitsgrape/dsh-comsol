"""Unit tests for snapshots (using a fake model that writes a real file)."""

from __future__ import annotations

from pathlib import Path

from comsol_mcp.snapshot import SnapshotManager


class FakeJavaModel:
    """Mimics the Java model: label() is both getter and setter."""

    def __init__(self):
        self._label = "capacitor_dc.mph"
        self.saved_to: list[str] = []

    def tag(self) -> str:
        return "model1"

    def label(self, value: str | None = None):
        if value is None:
            return self._label
        self._label = value


class FakeModel:
    """Minimal stand-in for mph.Model: `save` writes a marker file."""

    def __init__(self):
        self.java = FakeJavaModel()

    def save(self, path: str) -> None:
        self.java.saved_to.append(path)
        Path(path).write_text("model-bytes", encoding="utf-8")


def test_snapshot_writes_file_and_index(tmp_path: Path):
    manager = SnapshotManager(tmp_path)
    target = manager.snapshot(FakeModel(), "before-solve")

    assert target.exists()
    assert target.parent == tmp_path / "history"
    assert "before-solve" in target.name

    records = manager.list()
    assert len(records) == 1
    assert records[0]["tag"] == "model1"
    assert records[0]["file"] == str(target)


def test_snapshot_names_do_not_collide(monkeypatch):
    # freeze the clock so both snapshots want the same name
    import comsol_mcp.snapshot as snapshot_module

    class FixedTime:
        @staticmethod
        def strftime(_format: str) -> str:
            return "20260101-000000"

    monkeypatch.setattr(snapshot_module.time, "strftime", FixedTime.strftime)

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        manager = SnapshotManager(Path(folder))
        first = manager.snapshot(FakeModel(), "x")
        second = manager.snapshot(FakeModel(), "x")
        assert first != second
        assert first.exists() and second.exists()


def test_label_sanitization(tmp_path: Path):
    manager = SnapshotManager(tmp_path)
    target = manager.snapshot(FakeModel(), 'weird/name:"*?')
    assert target.exists()
    assert "/" not in target.name and '"' not in target.name


def test_prune_keeps_newest(tmp_path: Path, monkeypatch):
    import comsol_mcp.snapshot as snapshot_module

    counter = {"n": 0}

    def fake_strftime(_format: str) -> str:
        counter["n"] += 1
        return f"20260101-0000{counter['n']:02d}"

    monkeypatch.setattr(snapshot_module.time, "strftime", fake_strftime)
    manager = SnapshotManager(tmp_path)
    for index in range(5):
        manager.snapshot(FakeModel(), f"s{index}")

    removed = manager.prune(keep=2)
    assert removed == 3
    remaining = manager.list()
    assert len(remaining) == 2
    assert all(Path(record["file"]).exists() for record in remaining)
