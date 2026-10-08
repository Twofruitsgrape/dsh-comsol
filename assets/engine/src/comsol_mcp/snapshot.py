"""Model snapshots — never let the agent destroy the user's file.

Every write operation snapshots the main model first (see the tool layer).
Snapshots live in `<COMSOL_MCP_HOME>/history/` and are also listed for the
agent, so it can tell the user where to find a known-good state.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")


def _safe(name: str, fallback: str = "model") -> str:
    cleaned = _SAFE.sub("-", str(name or "")).strip("-")
    return cleaned or fallback


def save_copy(model, path: str, format: str | None = None) -> None:
    """Save a copy of the model without COMSOL's "save as" rename side effect.

    `model.save(path)` renames the model: its label and file association both
    become the new file name, which is why a snapshot used to make the COMSOL
    window title jump to the snapshot name (observed live). The label is
    restored here; the file association is deliberately left on the copy, so an
    accidental "save" in the COMSOL GUI writes to the copy and never to the
    user's original file.
    """
    java = model.java
    original_label = str(java.label())
    if format:
        model.save(path, format)
    else:
        model.save(path)
    try:
        if str(java.label()) != original_label:
            java.label(original_label)
    except Exception as exc:  # pragma: no cover - COMSOL quirk
        log.debug("Could not restore the model label after saving: %s", exc)


class SnapshotManager:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.dir = self.home / "history"
        self.index = self.dir / "index.jsonl"

    def snapshot(self, model, label: str | None = None) -> Path:
        """Save a copy of `model` (an `mph.Model`) and record it in the index."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{_safe(label or 'snapshot')}.mph"
        target = self.dir / name
        counter = 1
        while target.exists():
            target = self.dir / f"{stamp}-{_safe(label or 'snapshot')}-{counter}.mph"
            counter += 1
        save_copy(model, str(target))
        record = {
            "time": stamp,
            "label": label or "",
            "tag": str(model.java.tag()),
            "file": str(target),
            "size": target.stat().st_size if target.exists() else None,
        }
        try:
            with self.index.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("Could not write snapshot index: %s", exc)
        return target

    def list(self, limit: int = 20) -> list[dict]:
        if not self.index.exists():
            return []
        records: list[dict] = []
        try:
            for line in self.index.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return records[-limit:]

    def prune(self, keep: int = 50) -> int:
        """Delete the oldest snapshots beyond `keep` and rewrite the index."""
        records = self.list(limit=10_000)
        if len(records) <= keep:
            return 0
        removed = 0
        for record in records[:-keep]:
            path = Path(record.get("file", ""))
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        try:
            with self.index.open("w", encoding="utf-8") as handle:
                for record in records[-keep:]:
                    line = json.dumps(record, ensure_ascii=False)
                    handle.write(line + chr(10))
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("Could not rewrite snapshot index: %s", exc)
        return removed
