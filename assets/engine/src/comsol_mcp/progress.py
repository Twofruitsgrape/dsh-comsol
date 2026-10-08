"""Streaming COMSOL progress into the agent conversation.

COMSOL writes structured progress text (percentages, stages, memory, solver
iterations) to a file when `ModelUtil.showProgress(<file>)` is active. This
module tails that file so the MCP layer can emit real progress notifications
while a long mesh build or solve is running.

Note: the file mode and the desktop progress window are mutually exclusive —
the last `showProgress` call wins. `mode="stream"` therefore trades the
desktop's own progress dialog for progress the agent and user can read.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_PERCENT = re.compile(r"(\d+)\s*%")
# COMSOL writes section banners as `<---- <title> ----` when a section opens and
# `----- <title> ---->` when it closes. Only those two shapes are banners; the
# percentage lines also start with dashes and must not be mistaken for one.
_SECTION_OPEN = re.compile(r"^<[-]{2,}\s*(.*?)\s*[-]*$")
_SECTION_CLOSE = re.compile(r"^[-]{2,}\s*(.*?)\s*[-]*>$")
_STAGE = re.compile(r"%\s*-\s*(.+?)\s*$")
# COMSOL prints two pairs, e.g. "Memory: 1160/1192 1244/1279"; the first is
# the process currently doing the work.
_MEMORY = re.compile(r"Memory:\s*(\S+/\S+)")


class ProgressStream:
    """Tails one COMSOL progress-log file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._position = 0
        self._pending = b""

    def enable(self, client) -> None:
        """Ask the COMSOL server to log progress into our file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.reset()
        client.java.showProgress(str(self.path))

    def reset(self) -> None:
        self._position = 0
        self._pending = b""

    def read_new(self) -> list[str]:
        """Return the progress lines written since the previous call.

        Reads in binary and tracks a *byte* position: text-mode tell() after
        errors="replace" decodes desynchronises whenever a replacement char
        ate multi-byte data (GBK Chinese logs on a Chinese Windows). COMSOL
        writes the log in the system locale (GBK on zh_CN, UTF-8 elsewhere),
        so decode UTF-8 first and fall back to the ANSI code page.
        """
        if not self.path.exists():
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._position)
                data = handle.read()
                self._position = handle.tell()
        except OSError as exc:
            log.debug("Cannot read progress file %s: %s", self.path, exc)
            return []
        if not data:
            return []
        raw = self._pending + data
        *chunks, self._pending = raw.split(b"\n")
        return [line.rstrip() for line in
                (self._decode(chunk) for chunk in chunks) if line.strip()]

    @staticmethod
    def _decode(chunk: bytes) -> str:
        for encoding in ("utf-8", "gbk", "cp936"):
            try:
                return chunk.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return chunk.decode("utf-8", errors="replace")

    @staticmethod
    def parse(lines: list[str]) -> dict:
        """Summarise the newest progress information in `lines`."""
        percent: int | None = None
        stage: str | None = None
        section: str | None = None
        memory: str | None = None
        last_line: str | None = None
        for line in lines:
            match = _SECTION_OPEN.match(line) or _SECTION_CLOSE.match(line)
            if match:
                section = match.group(1).strip()
                last_line = line.strip()
                continue
            match = _PERCENT.search(line)
            if match:
                percent = int(match.group(1))
                stage_match = _STAGE.search(line)
                if stage_match:
                    stage = stage_match.group(1)
            match = _MEMORY.search(line)
            if match:
                memory = match.group(1)
            last_line = line.strip()
        return {
            "percent": percent,
            "stage": stage,
            "section": section,
            "memory": memory,
            "last_line": last_line,
        }

    @staticmethod
    def describe(state: dict) -> str:
        parts: list[str] = []
        if state.get("section"):
            parts.append(str(state["section"]))
        if state.get("percent") is not None:
            parts.append(f"{state['percent']}%")
        if state.get("stage"):
            parts.append(str(state["stage"]))
        return " | ".join(parts) if parts else (state.get("last_line") or "")
