"""
ss_persistence.py — Character persistence utilities for SentientSands.

After the Kayak-only migration, this module retains only:
  - Identity utilities (_clean_npc_name, _is_strong_uid)
  - Storage ID resolution (make_storage_id, extract_id_from_context)
  - Filename sanitization (_safe_char_filename)
  - Profile decision logic (should_save_profile, profile_needs_upgrade)
  - Name registry (get_used_names — Kayak-only, no filesystem)

All character I/O (load, save, cache) now lives in ss_character_gateway.py.
The gateway is the single read/write interface.

[Design: Pineaxe]
"""

import json
import logging
import os
import re
import sys


# ─── LAZY SERVER STATE ACCESSOR ──────────────────────────────────────────────

def _sv():
    """Return the live server module state (prefer __main__ when script-launched)."""
    _main = sys.modules.get("__main__")
    if _main and hasattr(_main, "ACTIVE_CAMPAIGN"):
        _main_file = str(getattr(_main, "__file__", "")).replace("\\", "/").lower()
        if _main_file.endswith("/kenshi_llm_server.py"):
            return _main
    _loaded = sys.modules.get("kenshi_llm_server")
    if _loaded and hasattr(_loaded, "ACTIVE_CAMPAIGN"):
        return _loaded
    import kenshi_llm_server as _s
    return _s


# ─── UTILITY FUNCTIONS ───────────────────────────────────────────────────────

def _clean_npc_name(name):
    """Strip pipe-separated IDs from NPC names."""
    if name is None:
        return ""
    clean = str(name).strip()
    if '|' in clean:
        clean = clean.split('|', 1)[0].strip()
    return clean


def _is_strong_uid(value):
    """Identify stable, unique IDs vs generic/volatile ones."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    lower = text.lower()
    if lower.startswith("hand_"):
        return False
    if text.isdigit():
        return False
    # New DLL builds expose persistent handles as 5 numeric parts joined by hyphens,
    # e.g. "1-304443360-1-2050292992-1".
    if re.fullmatch(r"\d+(?:-\d+){4}", text):
        return True
    # Fallbacks
    return (
        ("-" in text and len(text.split("-")) >= 3)
        or (text.count('-') == 4 and all(part.isdigit() for part in text.split('-')))
    )


def _safe_char_filename(storage_id):
    """Filesystem-safe stem for a character storage_id."""
    return "".join(
        [c for c in str(storage_id) if c.isalnum() or c in (' ', '_', '-')]
    ).strip()


# ─── STORAGE ID ──────────────────────────────────────────────────────────────

def make_storage_id(name, faction=None, context={}, runtime_id=""):
    """Build a collision-safe storage ID.

    Prefers Kenshi Object UUID when available, falls back to name+faction slug.
    When no strong UID exists, appends runtime_id as a disambiguation suffix
    to prevent same-name NPCs from overwriting each other's profiles.
    """
    uid = context.get('storage_id', context.get('persistent_id', "")) if isinstance(context, dict) else ""
    if _is_strong_uid(uid):
        return uid
    if _is_strong_uid(runtime_id):
        return runtime_id

    clean = str(name).strip()
    if '|' in clean:
        clean = clean.split('|')[0].strip()
    f = (faction or "").strip()
    if f and f not in ("Unknown", "None", "No Faction", ""):
        f_slug = re.sub(r'\s+', '_', re.sub(r'[^\w\s-]', '', f).strip())
        if f_slug:
            n_slug = re.sub(r'\s+', '_', clean)
            base = f"{n_slug}_{f_slug}"
        else:
            base = clean
    else:
        base = clean

    # Append runtime_id suffix when available to prevent same-name collisions
    _rid = ""
    if isinstance(context, dict):
        _rid = str(context.get("runtime_id") or context.get("id") or "").strip()
    if not _rid:
        _rid = str(runtime_id or "").strip()
    # Only append if it looks like a real runtime id (not just the name repeated)
    if _rid and _rid != clean and not _rid.lower().startswith("hand_"):
        return f"{base}_{_rid}"
    return base


def extract_id_from_context(context_json):
    """Extract the best available identity from a context dict or JSON string."""
    if not context_json:
        return None
    try:
        if isinstance(context_json, str) and context_json.strip().startswith('{'):
            try:
                ctx = json.loads(context_json)
            except Exception:
                return None
        elif isinstance(context_json, dict):
            ctx = context_json
        else:
            return None

        if isinstance(ctx, dict):
            persistent_id = ctx.get('persistent_id')
            if _is_strong_uid(persistent_id):
                return persistent_id
            return (
                ctx.get('runtime_id')
                or ctx.get('id')
                or ctx.get('ID')
                or ctx.get('storage_id')
            )
    except Exception:
        pass
    return None


# ─── PROFILE DECISIONS ────────────────────────────────────────────────────────

def should_save_profile(name, storage_id, data):
    """Checks if we should save this profile, preventing generic clutter."""
    if not name or name in ("Unknown", "Someone"):
        return False

    personality = data.get("Personality", "").lower()
    is_generic_content = any(
        x in personality
        for x in (
            "unknown",
            "generic npc",
            "weary wanderer",
            "weary traveler",
            "quiet traveler",
            "you do not know this person yet",
        )
    )
    has_history = len(data.get("ConversationHistory", [])) > 0

    if profile_needs_upgrade(data) and not has_history:
        return False

    if is_generic_content and not has_history:
        return False

    return True


def is_placeholder_profile(data):
    """Heuristic check for old stub biographies that should never be treated as final."""
    if not data:
        return False

    state = str(data.get("_profile_state") or "").strip().lower()
    if state == "pending_intro":
        return True

    personality = str(data.get("Personality") or "").strip().lower()
    backstory = str(data.get("Backstory") or "").strip()
    backstory_low = backstory.lower()

    if personality in {
        "a quiet traveler.",
        "a quiet traveler who keeps to themselves.",
        "a generic npc.",
        "you do not know this person yet.",
    }:
        return True

    if personality.startswith("a quiet traveler"):
        return True

    if not personality and backstory_low in {"", "unknown.", "their past is unclear."}:
        return True

    if personality in {"a quiet traveler.", "a quiet traveler who keeps to themselves."}:
        if re.fullmatch(r"a .+ from .+\.", backstory_low):
            return True

    return False


def profile_needs_upgrade(data):
    """Return True when a saved profile is still a placeholder or pending first contact."""
    return bool(
        data
        and (
            data.get("_transient")
            or str(data.get("_profile_state") or "").strip().lower() == "pending_intro"
            or is_placeholder_profile(data)
        )
    )


# ─── NAME REGISTRY ────────────────────────────────────────────────────────────

def get_used_names():
    """Return the set of all NPC names (lowercase).

    Queries Kayak index via Hub. No filesystem scan.
    """
    sv = _sv()
    hub = sv.kayak_hub
    ACTIVE_CAMPAIGN = sv.ACTIVE_CAMPAIGN

    if not hub:
        logging.warning("USED_NAMES: Hub not available, returning empty set")
        return set()

    try:
        from bridges.kayak_keys import Ops
        result = hub.execute({
            "op": Ops.LIST_CHARACTERS,
            "campaign": ACTIVE_CAMPAIGN,
        })
        if result.get("ok"):
            names = result.get("data", [])
            return {str(n).lower() for n in names if n}
        else:
            logging.warning(f"USED_NAMES: Hub error: {result.get('errors')}")
            return set()
    except Exception as e:
        logging.error(f"USED_NAMES: Hub call failed: {e}")
        return set()


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

__all__ = [
    "_clean_npc_name",
    "_is_strong_uid",
    "_safe_char_filename",
    "make_storage_id",
    "extract_id_from_context",
    "should_save_profile",
    "is_placeholder_profile",
    "profile_needs_upgrade",
    "get_used_names",
]
