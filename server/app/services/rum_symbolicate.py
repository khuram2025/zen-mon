"""Browser stack-trace symbolication from uploaded source maps.

Dependency-free: the Source Map v3 ``mappings`` field is decoded here
(base64 VLQ, https://tc39.es/ecma426/) so the OTA ``pip_install`` step can
never break it. Maps are looked up by the *minified file name* the browser
loaded (``app.3f2a1c.js``) within the application and release the error was
reported for; a map uploaded with an empty release applies to any release.

Only the frames are rewritten. The raw stack is always returned alongside so
an operator can see what the browser actually reported.
"""
from __future__ import annotations

import bisect
import gzip
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

_V8_FRAME = re.compile(r"^\s*at\s+(?:(?P<fn>.+?)\s+\()?(?P<url>[^()]+?):(?P<line>\d+)(?::(?P<col>\d+))?\)?\s*$")
_GECKO_FRAME = re.compile(r"^\s*(?P<fn>[^@\s]*)@(?P<url>.+?):(?P<line>\d+)(?::(?P<col>\d+))?\s*$")
_B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}
MAX_CONTEXT_LINES = 3


@dataclass
class Frame:
    raw: str
    function: str = ""
    url: str = ""
    line: int | None = None
    column: int | None = None
    file_name: str = ""
    original: dict | None = None      # {source, line, column, name}
    context: list[dict] = field(default_factory=list)  # [{line, code, current}]
    symbolicated: bool = False


def parse_stack(stack: str) -> list[Frame]:
    """Frames of a V8 (Chrome, Edge, Node) or Gecko / WebKit stack string."""
    frames: list[Frame] = []
    for raw in (stack or "").splitlines():
        if not raw.strip():
            continue
        match = _V8_FRAME.match(raw) or _GECKO_FRAME.match(raw)
        if not match:
            frames.append(Frame(raw=raw))
            continue
        url = match.group("url").strip()
        frames.append(Frame(
            raw=raw,
            function=(match.group("fn") or "").strip() or "<anonymous>",
            url=url,
            line=int(match.group("line")),
            # Some browsers (and hand-written test stacks) omit the column; map
            # lookups then take the first segment of the line.
            column=int(match.group("col")) if match.group("col") else 1,
            file_name=file_name_of(url),
        ))
    return frames


def file_name_of(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    return path.rsplit("/", 1)[-1].split("?")[0]


def _decode_vlq(segment: str) -> list[int]:
    values: list[int] = []
    shift = 0
    value = 0
    for char in segment:
        digit = _B64[char]
        value |= (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        values.append(-(value >> 1) if value & 1 else value >> 1)
        shift = 0
        value = 0
    return values


class SourceMap:
    """Decoded v3 map: per generated line, a sorted list of segments."""

    def __init__(self, payload: dict):
        self.sources: list[str] = list(payload.get("sources") or [])
        root = payload.get("sourceRoot") or ""
        if root:
            self.sources = [root.rstrip("/") + "/" + s if not s.startswith(("/", "http")) else s for s in self.sources]
        self.names: list[str] = list(payload.get("names") or [])
        self.contents: list[str | None] = list(payload.get("sourcesContent") or [])
        self._lines: list[list[tuple[int, int, int, int, int]]] = []
        self._decode(payload.get("mappings") or "")

    def _decode(self, mappings: str) -> None:
        source = original_line = original_column = name = 0
        for group in mappings.split(";"):
            generated_column = 0
            segments: list[tuple[int, int, int, int, int]] = []
            for raw in group.split(","):
                if not raw:
                    continue
                values = _decode_vlq(raw)
                generated_column += values[0]
                if len(values) >= 4:
                    source += values[1]
                    original_line += values[2]
                    original_column += values[3]
                    if len(values) >= 5:
                        name += values[4]
                    segments.append((generated_column, source, original_line, original_column, name if len(values) >= 5 else -1))
            segments.sort()
            self._lines.append(segments)

    def lookup(self, line: int, column: int) -> dict | None:
        """Original position for a 1-based generated line / column."""
        index = line - 1
        if index < 0 or index >= len(self._lines) or not self._lines[index]:
            return None
        segments = self._lines[index]
        position = bisect.bisect_right([s[0] for s in segments], max(column - 1, 0)) - 1
        if position < 0:
            return None
        _gen, source, original_line, original_column, name = segments[position]
        if source < 0 or source >= len(self.sources):
            return None
        return {
            "source": self.sources[source],
            "line": original_line + 1,
            "column": original_column + 1,
            "name": self.names[name] if 0 <= name < len(self.names) else "",
            "_source_index": source,
        }

    def context(self, source_index: int, line: int) -> list[dict]:
        if source_index >= len(self.contents) or not self.contents[source_index]:
            return []
        lines = self.contents[source_index].splitlines()
        start = max(0, line - 1 - MAX_CONTEXT_LINES)
        end = min(len(lines), line + MAX_CONTEXT_LINES)
        return [
            {"line": number + 1, "code": lines[number][:300], "current": number + 1 == line}
            for number in range(start, end)
        ]


@lru_cache(maxsize=16)
def _decoded(map_id: str, gzipped: bytes) -> SourceMap:
    return SourceMap(json.loads(gzip.decompress(gzipped)))


def decoded_map(map_id: str, gzipped: bytes) -> SourceMap:
    return _decoded(map_id, gzipped)


def symbolicate(stack: str, maps_by_file: dict[str, tuple[str, bytes]]) -> tuple[list[Frame], int]:
    """Rewrite frames whose minified file has an uploaded map.

    ``maps_by_file`` maps the minified file name to ``(map_id, gzipped map)``.
    Returns the frames and how many were symbolicated.
    """
    frames = parse_stack(stack)
    resolved = 0
    for frame in frames:
        if frame.line is None or frame.column is None or not frame.file_name:
            continue
        entry = maps_by_file.get(frame.file_name)
        if not entry:
            continue
        try:
            source_map = decoded_map(*entry)
            original = source_map.lookup(frame.line, frame.column)
        except Exception:
            original = None
        if not original:
            continue
        source_index = original.pop("_source_index")
        frame.original = original
        frame.context = source_map.context(source_index, int(original["line"]))
        frame.symbolicated = True
        resolved += 1
    return frames, resolved


def frame_payload(frame: Frame) -> dict:
    return {
        "raw": frame.raw,
        "function": frame.function,
        "url": frame.url,
        "line": frame.line,
        "column": frame.column,
        "file_name": frame.file_name,
        "symbolicated": frame.symbolicated,
        "original": frame.original,
        "context": frame.context,
    }
