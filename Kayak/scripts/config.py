# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak config loader.
Reads core_config.txt from Kayak/config/.
Format: key = value (lines starting with # are comments, blank lines ignored).
"""

import os
from typing import Any, Dict, Set

DEFAULTS: Dict[str, Any] = {
    # Retrieval limits
    "max_keywords":           5,
    "max_layers":             3,
    "max_matches_per_layer":  5,
    "max_files":              10,
    "timeout_ms":             1500,
    # Dialogue
    "dialogue_keep_lines":    30,
    "prompt_dialogue_keep_lines": 30,
    "yell_dialogue_keep_lines":   20,
    # Server
    "host":                   "127.0.0.1",
    "port":                   5001,
    # Campaign
    "default_campaign":       "Default",
    # Economy
    "price_modifier":          1.0,
    # Keyword indexing policy
    # Keep prose out of keyword matching by default to avoid noisy retrieval.
    "index_text_fields":       False,
    "index_free_text":         False,
    # Fields that are identity/structural — never used to expand layers.
    # Everything else is a valid expansion candidate.
    "non_expand_fields": (
        "id, persistent_id, runtime_id, weight, display_name, "
        "aliases, description, bio, personality_summary, average_price"
    ),
}

_FLOAT_KEYS: Set[str] = {
    "price_modifier",
}

_INT_KEYS: Set[str] = {
    "max_keywords", "max_layers", "max_matches_per_layer",
    "max_files", "timeout_ms", "dialogue_keep_lines",
    "prompt_dialogue_keep_lines", "yell_dialogue_keep_lines", "port",
}

_BOOL_KEYS: Set[str] = {
    "index_text_fields",
    "index_free_text",
}


def load_config(config_dir: str) -> Dict[str, Any]:
    cfg: Dict[str, Any] = dict(DEFAULTS)
    path = os.path.join(config_dir, "core_config.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    return _coerce(cfg)


def _coerce(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for k in _INT_KEYS:
        if k in cfg:
            try:
                cfg[k] = int(cfg[k])
            except (ValueError, TypeError):
                cfg[k] = DEFAULTS[k]

    for k in _FLOAT_KEYS:
        if k in cfg:
            try:
                cfg[k] = float(cfg[k])
            except (ValueError, TypeError):
                cfg[k] = DEFAULTS[k]

    for k in _BOOL_KEYS:
        if k in cfg:
            raw = cfg[k]
            if isinstance(raw, bool):
                continue
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "on"):
                cfg[k] = True
            elif s in ("0", "false", "no", "off", ""):
                cfg[k] = False
            else:
                cfg[k] = DEFAULTS[k]

    # non_expand_fields → frozenset of lowercase strings
    nef = cfg.get("non_expand_fields", "")
    if isinstance(nef, str):
        cfg["non_expand_fields"] = frozenset(
            f.strip().lower() for f in nef.split(",") if f.strip()
        )
    return cfg
