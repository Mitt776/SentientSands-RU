# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak event parser.

Reads a messy raw event log written by the game/mod and splits it into
clean, type-specific files:
  battle_events.txt
  trade_events.txt
  dialogue_events.txt
  world_events.txt
  misc_events.txt

The raw log is archived to IGN_raw_backup/ after parsing (never deleted).

Usage:
    from scripts.event_parser import parse_event_log
    parse_event_log("/path/to/raw_event_log.txt", "/path/to/output/")
"""

import os
import re
from datetime import datetime
from typing import Dict, List


_EVENT_PATTERNS: Dict[str, List[str]] = {
    "battle":   ["attack", "kill", "killed", "wound", "fight", "assault",
                 "raid", "war", "siege", "defeat", "victory", "bleed", "dead"],
    "trade":    ["buy", "sell", "trade", "purchase", "price", "shop",
                 "vendor", "merchant", "cats", "cost", "sold", "bought"],
    "dialogue": ["say", "said", "speak", "spoke", "tell", "told",
                 "ask", "asked", "reply", "replied", "respond", "greet"],
    "world":    ["discover", "arrive", "travel", "found", "explore",
                 "map", "location", "region", "enter", "leave", "reach"],
}
_ALL_TYPES = list(_EVENT_PATTERNS.keys()) + ["misc"]


def parse_event_log(raw_log_path: str, output_dir: str, backup: bool = True):
    """
    Parse and split a raw event log.

    Args:
        raw_log_path : path to raw log file
        output_dir   : directory where typed sub-logs will be written (appended)
        backup       : archive raw log to IGN_raw_backup/ before deleting
    """
    if not os.path.isfile(raw_log_path):
        return

    with open(raw_log_path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = [_clean(l) for l in f if l.strip()]

    if not raw_lines:
        return

    os.makedirs(output_dir, exist_ok=True)

    buckets: Dict[str, List[str]] = {t: [] for t in _ALL_TYPES}

    for line in raw_lines:
        ll = line.lower()
        matched = False
        for etype, keywords in _EVENT_PATTERNS.items():
            if any(kw in ll for kw in keywords):
                buckets[etype].append(line)
                matched = True
                break
        if not matched:
            buckets["misc"].append(line)

    for etype, lines in buckets.items():
        if not lines:
            continue
        out_path = os.path.join(output_dir, f"{etype}_events.txt")
        with open(out_path, "a", encoding="utf-8") as fout:
            fout.writelines(l + "\n" for l in lines)

    # Archive original
    if backup:
        bak_dir = os.path.join(os.path.dirname(raw_log_path), "IGN_raw_backup")
        os.makedirs(bak_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(bak_dir, f"event_log_{ts}.txt")
        os.rename(raw_log_path, dest)


def _clean(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line)
    return line
