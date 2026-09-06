# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak DSL normalizer.

Tolerantly parses entity.txt files into structured data.

Supports as input:
  key = value
  key: value
  key -> value
  &> SectionHeader        → converted to section_header: SectionHeader
  [something]             → skipped (bracket-only lines are tags, not data)
  plain free text lines   → collected as free_text

Every entity.txt MUST start with the three reserved header lines in order:
  Category = ...
  Name     = ...
  Id       = ...

These are read structurally and are NOT indexed as searchable keywords.
The rest of the file is body content.
"""

import re
from typing import Dict, List, Tuple

HEADER_KEYS = ("Category", "Name", "Id")
_KV_RE = re.compile(r'^([^:=\->\[\n]+?)\s*(?:->|:=|:(?!=)|=(?!=))\s*(.*)$')
_SECTION_RE = re.compile(r'^&>\s*(.+)')
_BRACKET_RE = re.compile(r'^\[.+\]$')


def parse_entity_file(path: str) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """
    Parse an entity.txt file.

    Returns:
        header    – dict with keys Category, Name, Id (whatever was found)
        fields    – dict of other key→value pairs (key lowercased, spaces→_)
        free_text – remaining unstructured lines joined by newline
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip("\n") for l in f]
    except OSError as e:
        import sys
        print(f"WARNING: Failed to read entity.txt: {path} — {e}", file=sys.stderr)
        return {}, {}, ""

    header: Dict[str, str] = {}
    fields: Dict[str, str] = {}
    free_lines: List[str] = []
    idx = 0

    # ── Read the three reserved header lines ─────────────────────────────
    for expected in HEADER_KEYS:
        while idx < len(lines) and not lines[idx].strip():
            idx += 1                          # skip blank lines at top
        if idx >= len(lines):
            break
        k, v = _parse_kv(lines[idx].strip())
        if k and k.lower() == expected.lower():
            header[expected] = v
        idx += 1  # Always advance to next line, whether it matched or not

    # ── Read the rest of the file ─────────────────────────────────────────
    current_key = ""
    current_value_lines: List[str] = []
    free_buffer: List[str] = []

    def _flush_field() -> None:
        nonlocal current_key, current_value_lines
        if current_key and current_key not in fields:
            fields[current_key] = _collapse_block(current_value_lines)
        current_key = ""
        current_value_lines = []

    def _flush_free() -> None:
        nonlocal free_buffer
        if not free_buffer:
            return
        block = _collapse_block(free_buffer)
        if block:
            free_lines.extend(block.splitlines())
        free_buffer = []

    body_lines = lines[idx:]
    for pos, raw in enumerate(body_lines):
        stripped = raw.strip()

        if _SECTION_RE.match(stripped) or _BRACKET_RE.match(stripped):
            _flush_field()
            _flush_free()
            continue

        k, v = _parse_kv(stripped) if stripped else ("", "")
        if k and not _is_reserved(k):
            _flush_field()
            _flush_free()
            current_key = k.strip().lower().replace(" ", "_")
            current_value_lines = [v]
            continue

        if not stripped:
            next_meaningful = ""
            for future in body_lines[pos + 1:]:
                future_stripped = future.strip()
                if future_stripped:
                    next_meaningful = future_stripped
                    break

            if current_key and _supports_multiline_value(current_key):
                next_k, _ = _parse_kv(next_meaningful) if next_meaningful else ("", "")
                if next_meaningful and not _SECTION_RE.match(next_meaningful) and not _BRACKET_RE.match(next_meaningful) and not (next_k and not _is_reserved(next_k)):
                    current_value_lines.append("")
                else:
                    _flush_field()
            elif current_key:
                _flush_field()
            elif free_buffer:
                next_k, _ = _parse_kv(next_meaningful) if next_meaningful else ("", "")
                if next_meaningful and not _SECTION_RE.match(next_meaningful) and not _BRACKET_RE.match(next_meaningful) and not (next_k and not _is_reserved(next_k)):
                    free_buffer.append("")
                else:
                    _flush_free()
            continue

        if current_key and _supports_multiline_value(current_key):
            current_value_lines.append(stripped)
        elif current_key:
            _flush_field()
            free_buffer.append(stripped)
        else:
            free_buffer.append(stripped)

    _flush_field()
    _flush_free()
    return header, fields, "\n".join(free_lines)


# ─────────────────────────────────────────────────────────────────────────────

def _parse_kv(line: str) -> Tuple[str, str]:
    m = _KV_RE.match(line)
    if not m:
        return "", ""
    k, v = m.group(1).strip(), m.group(2).strip()
    # Strip surrounding quotes
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return (k, v) if k else ("", "")


def _is_reserved(key: str) -> bool:
    return key.lower() in {h.lower() for h in HEADER_KEYS}


def _collapse_block(lines: List[str]) -> str:
    cleaned = [str(line).rstrip() for line in lines]
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


def _supports_multiline_value(key: str) -> bool:
    return str(key or "").startswith("$")
