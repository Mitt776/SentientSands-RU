"""
ss_identity.py — NPC identity resolution for SentientSands.

Owns all live context tracking, identity normalisation, storage ID
resolution, primary target selection, and direct/ambient chat state.

These functions answer the question: "who exactly are we talking to?"
They sit between the raw C++ JSON payload and the character data lookup.

Globals owned here (server imports them back):
  LIVE_CONTEXTS, LIVE_NAME_INDEX, PLAYER_CONTEXT   — dict, mutated in place
  AMBIENT_SPEAKER_LAST_AT                           — dict, mutated in place
  _istate                                           — namespace for scalar state

Extracted from kenshi_llm_server.py — [Design: Pineaxe]
"""

import json
import logging
import os
import re
import sys
import threading
import time

from configuration import load_settings
from ss_persistence import _clean_npc_name, _is_strong_uid
from personality_rules import (
    ANIMAL_RACES,
    MACHINE_RACES,
    SAPIENT_RACES,
    FERAL_RACE_KEYWORDS,
    is_known_race,
    race_matches_any,
)

_UNKNOWN_RACE_LOCK = threading.Lock()
_UNKNOWN_RACES_SEEN = set()


def _log_unrecognized_race(race, faction="", name="", source=""):
    race_text = str(race or "").strip()
    if not race_text:
        return
    key = (
        race_text.lower(),
        str(faction or "").strip().lower(),
        str(source or "").strip().lower(),
    )
    with _UNKNOWN_RACE_LOCK:
        if key in _UNKNOWN_RACES_SEEN:
            return
        _UNKNOWN_RACES_SEEN.add(key)
    entry = (
        f'UNRECOGNIZED_RACE: race="{race_text}" '
        f'name="{str(name or "").strip()}" '
        f'faction="{str(faction or "").strip()}" '
        f'source="{str(source or "").strip()}"'
    )
    logging.warning(entry)
    try:
        server_dir = getattr(_sv(), "KENSHI_SERVER_DIR", "")
        if server_dir:
            log_dir = os.path.join(server_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "unrecognized_races.log"), "a", encoding="utf-8") as f:
                f.write(entry + "\n")
    except Exception:
        pass


def get_persona_category(race, faction, name="", source=""):
    """Categorizes an NPC for persona and system prompt overrides."""
    race_l = (race or "").lower()
    if race_matches_any(race_l, MACHINE_RACES):
        return "machine"
    if race_matches_any(race_l, ANIMAL_RACES):
        return "animal"
    if race_matches_any(race_l, FERAL_RACE_KEYWORDS):
        return "feral"
    if race_matches_any(race_l, SAPIENT_RACES):
        return "sapient"
    if race_l and not is_known_race(race_l):
        _log_unrecognized_race(race, faction=faction, name=name, source=source)
    return "sapient"

# ─── LAZY SERVER STATE ACCESSOR ──────────────────────────────────────────────

def _sv():
    """Return the live server module state (prefer __main__ when script-launched)."""
    _main = sys.modules.get("__main__")
    if _main and hasattr(_main, "ACTIVE_CAMPAIGN") and hasattr(_main, "CHARACTERS_DIR"):
        _main_file = str(getattr(_main, "__file__", "")).replace("\\", "/").lower()
        if _main_file.endswith("/kenshi_llm_server.py"):
            return _main
    _loaded = sys.modules.get("kenshi_llm_server")
    if _loaded and hasattr(_loaded, "ACTIVE_CAMPAIGN") and hasattr(_loaded, "CHARACTERS_DIR"):
        return _loaded
    import kenshi_llm_server as _s
    return _s


# ─── OWNED GLOBALS ───────────────────────────────────────────────────────────
# Dict globals: mutated in place — safe to share via import (object ref is stable)

LIVE_CONTEXTS: dict = {}
LIVE_NAME_INDEX: dict = {}
PLAYER_CONTEXT: dict = {}
AMBIENT_SPEAKER_LAST_AT: dict = {}

# Character record of the player character currently selected in-game, posted by
# the SelectedSpeaker RE_Kenshi plugin. Holds ONLY per-character keys (name, ids,
# race, gender, faction, medical, stats, inventory...) — never world state such as
# day/hour/environment/events, which keep coming from the stock DLL's /context feed.
# Empty means "no selection reported"; the mod then behaves exactly as before.
SELECTED_PLAYER_CONTEXT: dict = {}

# Scalar state: wrapped in a namespace object so assignment doesn't break
# cross-module references. Server accesses via ss_identity._istate.xxx
class _IdentityState:
    last_npc_key            = None
    last_npc_name           = None
    last_direct_chat_key    = None
    last_direct_chat_at     = 0.0
    active_direct_chat_count = 0
    chat_priority_until     = 0.0
    selected_updated_at     = 0.0
    speaker_command_name    = None
    chat_nearby             = None

_istate = _IdentityState()

# Constants (read-only — no sharing problem)
AMBIENT_DIRECT_CHAT_COOLDOWN = 180.0
AMBIENT_SPEAKER_COOLDOWN     = 180.0
DIRECT_CHAT_GRACE_SECONDS    = 2.0

# How long a selection report stays authoritative. The plugin re-posts every ~2s,
# so anything older than this means the plugin stopped, the game closed, or RE_Kenshi
# unloaded it — in which case we fall back to the stock speaker instead of pinning
# the conversation to a character the player may have long since stopped using.
SELECTED_TTL_SECONDS = 10.0

# Locks defined here, exported to server
PRIORITY_LOCK = threading.Lock()
# Guards LIVE_CONTEXTS and LIVE_NAME_INDEX together. They are two halves of one
# structure -- the index points into the dict -- and Flask runs threaded, so
# /context, /chat and every listener thread mutate them concurrently. Reentrant
# because store_live_context() calls resolve_live_context() on the way through.
IDENTITY_LOCK = threading.RLock()
SELECTED_LOCK = threading.Lock()


# ─── SELECTED SPEAKER ────────────────────────────────────────────────────────
# The stock SentientSands.dll always reports playerCharacters[0] as the speaker.
# The SelectedSpeaker plugin reports whichever player character is actually
# selected in-game; these helpers layer that report over PLAYER_CONTEXT so every
# caller asks one question — "who is speaking right now?" — instead of reading
# the raw global. With no plugin (or a stale/disabled report) they return the
# stock values unchanged, so the mod degrades to its previous behaviour.

def selected_speaker_enabled():
    try:
        return bool(load_settings().get("enable_selected_speaker", True))
    except Exception:
        return True


def set_selected_player_context(data):
    """Replace the reported selection. Falsy or empty data clears it."""
    with SELECTED_LOCK:
        SELECTED_PLAYER_CONTEXT.clear()
        if isinstance(data, dict) and data:
            SELECTED_PLAYER_CONTEXT.update(data)
            _istate.selected_updated_at = time.monotonic()
        else:
            _istate.selected_updated_at = 0.0
        return dict(SELECTED_PLAYER_CONTEXT)


def selected_is_fresh():
    """True when a selection was reported recently and the feature is on."""
    if not SELECTED_PLAYER_CONTEXT or not _istate.selected_updated_at:
        return False
    if (time.monotonic() - _istate.selected_updated_at) > SELECTED_TTL_SECONDS:
        return False
    return selected_speaker_enabled()


# ─── SPEAKER CHOSEN BY THE /speaker CHAT COMMAND ────────────────────────────
# Second source for the same override, for players without the plugin. Only a
# NAME is remembered, deliberately: the character's record is rebuilt from the
# live `nearby` feed on every request, so their health and gear stay current
# instead of freezing at whatever they were when the command was typed.
#
# Unlike the plugin's reports this has no TTL. Nothing re-sends it, so expiring
# it would silently drop the player's choice mid-conversation.

# Fields the stock DLL puts in each `nearby` entry. Everything else about a
# squad member — exact hunger, blood, stats, carried items — is simply not
# transmitted for anyone but squad slot 1.
_NEARBY_IDENTITY_KEYS = (
    "race", "gender", "faction", "factionID", "origin_faction",
    "persistent_id", "runtime_id", "storage_id", "id",
)

# Per-body values we cannot know for a non-slot-1 character. Blanked rather than
# inherited: attributing squad slot 1's starvation or backpack to someone else
# reads as fact to the LLM and is worse than saying nothing.
_UNKNOWABLE_FOR_COMMAND_SPEAKER = ("medical", "stats", "inventory")

# Handles that address a specific body in Kayak. These MUST be overwritten on
# every override, never inherited through the shallow merge.
#
# Kayak's resolve_player_entity_object() tries ids before names, so one stale
# handle silently outranks the new speaker's name: the sheet header reads as the
# chosen character while the personality, backstory and speech quirks come from
# whoever the old id points at. Blanking makes Kayak fall through to name
# resolution, which finds the right entity. "ID" is listed because Kayak checks
# that spelling too.
_SPEAKER_ID_KEYS = ("persistent_id", "runtime_id", "storage_id", "id", "ID")

# `nearby` carries condition as one of the game's own status words. Mapping it
# onto character_state matters: the prompt builders treat an empty medical block
# as "healthy", so without this a wounded squad member would be described as
# perfectly fine. "Healthy" maps to normal so it falls through to that default.
_HEALTH_TEXT_TO_STATE = {
    "dead": "dead",
    "unconscious": "unconscious",
    "playing dead": "unconscious",
    "crippled": "crippled",
    "injured": "injured",
    "healthy": "normal",
}


def remember_chat_nearby(nearby):
    """Stash the /chat payload's nearby list as a fallback identity source.

    Kept in _istate rather than PLAYER_CONTEXT because POST /context prunes any
    key the DLL stopped sending, which would silently drop it.
    """
    _istate.chat_nearby = nearby if isinstance(nearby, list) else None


def _iter_nearby():
    """Nearby entries from the richest feed available.

    POST /context carries health and equipment per entry; the /chat payload does
    not. Prefer the former and fall back to the last chat request's list.
    """
    for source in (PLAYER_CONTEXT.get("nearby"), _istate.chat_nearby):
        if isinstance(source, list) and source:
            return source
    return []


def list_squad_names():
    """Squad roster for the /speaker listing, in the game's own order."""
    names = []
    for raw in (PLAYER_CONTEXT.get("squad") or []):
        text = str(raw or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _find_nearby_entry(name):
    """Match a squad member in the nearby feed: exact first, then prefix."""
    target = _clean_npc_name(name).lower()
    if not target:
        return None
    entries = [e for e in _iter_nearby() if isinstance(e, dict)]
    for entry in entries:
        if _clean_npc_name(entry.get("name")).lower() == target:
            return entry
    partial = [e for e in entries
               if target in _clean_npc_name(e.get("name")).lower()]
    return partial[0] if len(partial) == 1 else None


def resolve_speaker_candidate(name):
    """Return the canonical name for a /speaker argument, or None.

    The squad roster is the only authority on who may speak — `nearby` is not,
    since it lists every character around the player, hostile ones included.
    Matching a name there would happily hand the player's voice to a passing
    Holy Nation paladin. `nearby` is used purely to flesh out the record once
    membership has been established here.
    """
    target = _clean_npc_name(name).lower()
    roster = list_squad_names()
    if not target or not roster:
        return None
    for candidate in roster:
        if _clean_npc_name(candidate).lower() == target:
            return candidate
    partial = [c for c in roster if target in _clean_npc_name(c).lower()]
    return partial[0] if len(partial) == 1 else None


def _command_speaker_record():
    """Build the chosen speaker's record from the live nearby feed."""
    name = _istate.speaker_command_name
    if not name:
        return {}

    record = {"name": name}
    # Seed every handle blank so the merge cannot leave the stock speaker's id
    # in place. Anything the nearby feed actually knows overwrites these below.
    for key in _SPEAKER_ID_KEYS:
        record[key] = ""
    entry = _find_nearby_entry(name)
    if entry:
        for key in _NEARBY_IDENTITY_KEYS:
            value = entry.get(key)
            if value not in (None, ""):
                record[key] = value
        # health/equipment arrive as prose, not the structured blocks the
        # prompts read; carry them under their own keys rather than faking
        # medical{} numbers we do not have.
        for src, dst in (("health", "health_text"), ("equipment", "equipment_text")):
            value = str(entry.get(src) or "").strip()
            if value:
                record[dst] = value

        health_text = str(entry.get("health") or "").strip().lower()
        if health_text:
            record["character_state"] = _HEALTH_TEXT_TO_STATE.get(health_text, health_text)
    for key in _UNKNOWABLE_FOR_COMMAND_SPEAKER:
        record[key] = [] if key == "inventory" else {}
    return record


def set_speaker_by_name(name):
    """Pin the speaker by name.

    Returns (status, resolved_name):
      "ok"           — speaker changed
      "reverted"     — asked for squad slot 1, which is the stock speaker anyway
      "not_in_squad" — no roster match (or the roster has not arrived yet)
      "not_nearby"   — on the roster but absent from the nearby feed, so their
                       race, gender and faction are unknown to us. Refused
                       rather than accepted, because inheriting slot 1's body
                       would describe a Shek as a Greenlander.
    """
    resolved = resolve_speaker_candidate(name)
    if not resolved:
        return ("not_in_squad", None)

    # Slot 1 is what the stock feed already describes — dropping the override
    # is both simpler and more complete than rebuilding them from `nearby`.
    stock_name = str((PLAYER_CONTEXT or {}).get("name") or "").strip()
    if stock_name and _clean_npc_name(resolved).lower() == _clean_npc_name(stock_name).lower():
        clear_speaker_command()
        return ("reverted", resolved)

    if not _find_nearby_entry(resolved):
        return ("not_nearby", resolved)

    with SELECTED_LOCK:
        _istate.speaker_command_name = resolved
    return ("ok", resolved)


def clear_speaker_command():
    with SELECTED_LOCK:
        had = _istate.speaker_command_name
        _istate.speaker_command_name = None
    return had


def command_speaker_active():
    return bool(_istate.speaker_command_name) and selected_speaker_enabled()


# ─── COMBINED VIEW ──────────────────────────────────────────────────────────

def get_effective_player_context():
    """PLAYER_CONTEXT with the acting speaker's record layered on top.

    Priority: a fresh plugin report, else a /speaker choice, else the stock
    squad-slot-1 record untouched.

    Shallow merge on purpose: both overrides carry only per-character keys, so
    world state (day, hour, gamespeed, environment, events, nearby) survives
    from the stock feed and cannot drift between sources.
    """
    merged = dict(PLAYER_CONTEXT or {})
    with SELECTED_LOCK:
        if selected_is_fresh():
            merged.update(SELECTED_PLAYER_CONTEXT)
        elif command_speaker_active():
            merged.update(_command_speaker_record())
    return merged


def get_effective_player_name(fallback="Drifter"):
    """Name of the character that should be speaking, else the caller's fallback."""
    with SELECTED_LOCK:
        if selected_is_fresh():
            name = str(SELECTED_PLAYER_CONTEXT.get("name") or "").strip()
            if name:
                return name
        elif command_speaker_active():
            return _istate.speaker_command_name
    # The caller's fallback is the DLL's separate "player" field, which the
    # binary patch does not touch — it still names squad 1 slot 1. PLAYER_CONTEXT,
    # on the other hand, is the character the DLL actually serialised, so its
    # name is the one that matches the race, health and inventory in the prompt.
    # Prefer it, so the sheet cannot describe one character and name another.
    ctx_name = str(PLAYER_CONTEXT.get("name") or "").strip()
    if ctx_name:
        return ctx_name
    return fallback


def selected_speaker_status():
    """Diagnostic snapshot for GET /context."""
    with SELECTED_LOCK:
        stamp = _istate.selected_updated_at
        plugin_fresh = selected_is_fresh()
        if plugin_fresh:
            source = "plugin"
        elif command_speaker_active():
            source = "command"
        else:
            source = "stock"
        return {
            "enabled": selected_speaker_enabled(),
            "source": source,
            "fresh": plugin_fresh,
            "age_seconds": round(time.monotonic() - stamp, 1) if stamp else None,
            "ttl_seconds": SELECTED_TTL_SECONDS,
            "command_speaker": _istate.speaker_command_name,
            "context": dict(SELECTED_PLAYER_CONTEXT),
        }


# ─── NAME/FACTION UTILITIES ──────────────────────────────────────────────────
# Defined here alongside identity functions. Server imports this back.

def _build_name_faction_id(name, faction=None):
    clean = _clean_npc_name(name)
    f = (faction or "").strip()
    if f and f not in ("Unknown", "None", "No Faction", ""):
        f_slug = re.sub(r'\s+', '_', re.sub(r'[^\w\s-]', '', f).strip())
        if f_slug:
            return f"{clean}_{f_slug}"
    return clean


# ─── IDENTITY FUNCTIONS ──────────────────────────────────────────────────────

def _normalize_context_identity(context, fallback_name=""):
    if not isinstance(context, dict):
        return {}

    normalized = dict(context)
    clean_name = _clean_npc_name(normalized.get("name") or normalized.get("Name") or fallback_name)
    faction = (
        normalized.get("faction")
        or normalized.get("Faction")
        or normalized.get("origin_faction")
        or normalized.get("OriginFaction")
        or normalized.get("factionID")
        or ""
    ).strip()
    persistent_id = str(
        normalized.get("persistent_id")
        or normalized.get("PersistentID")
        or ""
    ).strip() or None
    runtime_id = str(
        normalized.get("runtime_id")
        or normalized.get("id")
        or ""
    ).strip() or None
    storage_id = _sv()._preferred_storage_id(
        clean_name,
        faction,
        normalized.get("storage_id"),
        normalized.get("ID"),
        uid=persistent_id,
    )
    if runtime_id:
        normalized["runtime_id"] = runtime_id
    if persistent_id:
        normalized["persistent_id"] = persistent_id
    if storage_id:
        normalized["storage_id"] = storage_id
    return normalized

def _normalized_faction_key(faction):
    if not faction:
        return ""
    text = str(faction).strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("player's squad"):
        parts = text.split(":", 1)
        text = parts[1].strip() if len(parts) == 2 else text[len("player's squad"):].strip()
    return re.sub(r'\s+', ' ', text).strip().lower()

def _context_identity_summary(context, fallback_name=""):
    ctx = _parse_context_dict(context, fallback_name=fallback_name)
    if not ctx:
        return {}

    clean_name = _context_name(ctx, fallback_name)
    faction = _context_faction(ctx)
    runtime_id = str(ctx.get("runtime_id") or ctx.get("id") or "").strip() or None
    persistent_id = str(ctx.get("persistent_id") or "").strip() or None
    storage_id = str(ctx.get("storage_id") or ctx.get("ID") or "").strip() or None
    key = runtime_id or (persistent_id if _is_strong_uid(persistent_id) else None) or storage_id or clean_name

    try:
        dist = float(ctx.get("dist", ctx.get("player_dist", 9999.0)) or 9999.0)
    except Exception:
        dist = 9999.0

    strength = 0
    if clean_name:
        strength = 1
    if storage_id and storage_id != clean_name:
        strength = 2
    if runtime_id:
        strength = 3
    if _is_strong_uid(persistent_id):
        strength = 4

    category = get_persona_category(ctx.get("race"), faction)

    return {
        "name": clean_name,
        "faction": faction,
        "runtime_id": runtime_id,
        "persistent_id": persistent_id,
        "storage_id": storage_id,
        "key": key,
        "dist": dist,
        "strength": strength,
        "context": ctx,
        "persona_category": category,
    }

def _identity_values(payload):
    if not isinstance(payload, dict):
        return set()

    values = set()
    for key in ("runtime_id", "persistent_id", "storage_id", "id", "ID", "key"):
        text = str(payload.get(key) or "").strip()
        if text:
            values.add(text)
    return values

def _context_text_value(context, *keys):
    if not isinstance(context, dict):
        return ""
    for key in keys:
        value = context.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""

def context_is_dead(context):
    """Best-effort death check for runtime context payloads.

    The DLL payloads are not perfectly uniform across direct targets and nearby
    NPCs, so this accepts the explicit state flags when present and falls back
    to the rendered health/status strings used by nearby NPC awareness.
    """
    if not isinstance(context, dict):
        return False

    for key in ("is_dead", "dead", "IsDead"):
        value = context.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "dead"):
            return True

    state = _context_text_value(context, "character_state", "state", "status", "Status")
    if state and state.lower() in ("dead", "corpse"):
        return True

    health = _context_text_value(context, "health", "Health", "condition", "Condition")
    if health:
        lowered = health.lower()
        if re.search(r"\b(dead|corpse|deceased)\b", lowered):
            return True

    medical = context.get("medical")
    if isinstance(medical, dict):
        value = medical.get("is_dead") or medical.get("dead")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "dead"):
            return True

    return False

def _collect_target_candidates(clean_name, context=None, nearby_data=None):
    candidates = []
    seen = set()

    def _add_candidate(source, payload):
        summary = _context_identity_summary(payload, fallback_name=clean_name)
        if not summary or not summary.get("name"):
            return
        if clean_name and summary["name"] != clean_name:
            return

        key = summary.get("key") or summary["name"]
        if key in seen:
            return
        seen.add(key)
        summary["source"] = source
        candidates.append(summary)

    if context:
        _add_candidate("context", context)

    for npc in nearby_data or []:
        _add_candidate("nearby", npc)

    if clean_name:
        with IDENTITY_LOCK:
            live_hits = [LIVE_CONTEXTS[k] for k in LIVE_NAME_INDEX.get(clean_name, []) if k in LIVE_CONTEXTS]
        for live_ctx in live_hits:
            _add_candidate("live", live_ctx)

    return candidates

def resolve_primary_target(raw_name, context=None, nearby_data=None, mode="talk"):
    clean_name = _clean_npc_name(raw_name)
    explicit_runtime_id = str(raw_name).split('|', 1)[1].strip() if '|' in str(raw_name) else None
    context_summary = _context_identity_summary(context, fallback_name=clean_name)
    candidates = _collect_target_candidates(clean_name, context=context, nearby_data=nearby_data)

    def _finish(chosen, reason):
        if not chosen:
            return clean_name, context, explicit_runtime_id, None
        chosen_name = chosen.get("name") or clean_name
        chosen_ctx = chosen.get("context") or {}
        # Direct targeting should stay runtime-first; storage identity is for persistence.
        chosen_id = chosen.get("runtime_id") or chosen.get("storage_id")
        runtime_ref = f"{chosen_name}|{chosen.get('runtime_id')}" if chosen.get("runtime_id") else chosen_name
        logging.info(
            f"TARGET: Resolved '{clean_name or raw_name}' -> {chosen_name} "
            f"(key={chosen.get('key')}, source={chosen.get('source', 'fallback')}, reason={reason})"
        )
        return chosen_name, json.dumps(chosen_ctx), chosen_id, runtime_ref

    if explicit_runtime_id:
        for candidate in candidates:
            if explicit_runtime_id in (
                str(candidate.get("runtime_id") or ""),
                str(candidate.get("storage_id") or ""),
                str(candidate.get("key") or ""),
            ):
                return _finish(candidate, "explicit_runtime_id")

    # Guard against a bad direct context inheriting the target name via fallback
    # while carrying another nearby NPC's identity. If we have exactly one nearby
    # candidate for the requested name and its identity conflicts with the direct
    # context, trust the corroborated nearby candidate instead of the fallback-
    # named context blob.
    if context_summary and context_summary.get("strength", 0) >= 2:
        nearby_matches = [candidate for candidate in candidates if candidate.get("source") == "nearby"]
        if len(nearby_matches) == 1:
            ctx_ids = _identity_values(context_summary)
            nearby_ids = _identity_values(nearby_matches[0])
            if ctx_ids and nearby_ids and ctx_ids.isdisjoint(nearby_ids):
                logging.warning(
                    f"TARGET: Context identity conflict for '{clean_name}' "
                    f"(context={sorted(ctx_ids)}, nearby={sorted(nearby_ids)}). "
                    f"Preferring corroborated nearby target."
                )
                return _finish(nearby_matches[0], "nearby_conflict_guard")

    if len(candidates) == 1:
        return _finish(candidates[0], "unique_candidate")

    if context_summary and context_summary.get("strength", 0) >= 2:
        for candidate in candidates:
            if candidate.get("key") == context_summary.get("key"):
                return _finish(candidate, "strong_context")

    if _istate.last_direct_chat_key:
        recent_matches = [
            candidate for candidate in candidates
            if _istate.last_direct_chat_key in (
                str(candidate.get("key") or ""),
                str(candidate.get("storage_id") or ""),
                str(candidate.get("runtime_id") or ""),
            )
        ]
        if len(recent_matches) == 1:
            return _finish(recent_matches[0], "recent_direct_chat")

    player_faction = _normalized_faction_key(PLAYER_CONTEXT.get("faction"))
    if player_faction and mode in ("talk", "yell") and (not context_summary or context_summary.get("strength", 0) < 2):
        faction_matches = [
            candidate for candidate in candidates
            if _normalized_faction_key(candidate.get("faction")) == player_faction
        ]
        if len(faction_matches) == 1:
            return _finish(faction_matches[0], "player_faction")

    if len(candidates) > 1 and (not context_summary or context_summary.get("strength", 0) < 2):
        by_distance = sorted(candidates, key=lambda item: (item.get("dist", 9999.0), item.get("source") != "nearby"))
        if len(by_distance) == 1 or by_distance[0].get("dist", 9999.0) + 1.0 < by_distance[1].get("dist", 9999.0):
            return _finish(by_distance[0], "nearest_candidate")

    if context_summary:
        return _finish(context_summary, "context_fallback")

    if candidates:
        logging.warning(
            f"TARGET: Ambiguous target '{clean_name}'. "
            f"Candidates={[c.get('key') for c in candidates]}. Falling back to first candidate."
        )
        return _finish(candidates[0], "ambiguous_first")

    return clean_name, context, explicit_runtime_id, clean_name

def _parse_context_dict(context, fallback_name=""):
    if not context:
        return {}
    if isinstance(context, dict):
        return _normalize_context_identity(context, fallback_name=fallback_name)
    if isinstance(context, str) and context.strip().startswith('{'):
        try:
            parsed = json.loads(context)
            if isinstance(parsed, dict):
                return _normalize_context_identity(parsed, fallback_name=fallback_name)
        except Exception:
            return {}
    return {}

def _context_name(context, fallback_name=""):
    if not isinstance(context, dict):
        context = {}
    return _clean_npc_name(context.get("name") or context.get("Name") or fallback_name)

def _context_faction(context):
    if not isinstance(context, dict):
        context = {}
    return (
        context.get("faction")
        or context.get("Faction")
        or context.get("origin_faction")
        or context.get("OriginFaction")
        or context.get("factionID")
        or ""
    ).strip()

def _register_live_name(key, name):
    if not key or not name:
        return
    with IDENTITY_LOCK:
        bucket = LIVE_NAME_INDEX.setdefault(name, [])
        if key not in bucket:
            bucket.append(key)

def _unregister_live_aliases(key, ctx=None):
    with IDENTITY_LOCK:
        target = ctx or LIVE_CONTEXTS.get(key) or {}
        for alias in target.get("_aliases", []):
            bucket = LIVE_NAME_INDEX.get(alias)
            if not bucket:
                continue
            LIVE_NAME_INDEX[alias] = [candidate for candidate in bucket if candidate != key]
            if not LIVE_NAME_INDEX[alias]:
                del LIVE_NAME_INDEX[alias]

def resolve_live_context(name=None, context=None, explicit_id=None):
    """Find the cached live context for an NPC. Serialised against writers."""
    with IDENTITY_LOCK:
        return _resolve_live_context_locked(name, context, explicit_id)


def _resolve_live_context_locked(name=None, context=None, explicit_id=None):
    ctx_dict = _parse_context_dict(context, fallback_name=name)
    clean_name = _context_name(ctx_dict, name)
    faction = _context_faction(ctx_dict)

    candidates = []
    for candidate in (
        ctx_dict.get("runtime_id"),
        ctx_dict.get("id"),
        ctx_dict.get("persistent_id") if _is_strong_uid(ctx_dict.get("persistent_id")) else None,
        ctx_dict.get("storage_id"),
        ctx_dict.get("ID"),
        explicit_id,
        _build_name_faction_id(clean_name, faction) if clean_name else None,
        clean_name,
    ):
        if candidate:
            candidate = str(candidate)
            if candidate not in candidates:
                candidates.append(candidate)

    conflicting_hits = []
    for candidate in candidates:
        if candidate in LIVE_CONTEXTS:
            live_ctx = LIVE_CONTEXTS[candidate]
            live_name = _context_name(live_ctx)
            if clean_name and live_name and live_name != clean_name:
                conflicting_hits.append((candidate, live_ctx, live_name))
                continue
            return candidate, live_ctx

    if clean_name:
        live_keys = [k for k in LIVE_NAME_INDEX.get(clean_name, []) if k in LIVE_CONTEXTS]
        unique_keys = list(dict.fromkeys(live_keys))
        if len(unique_keys) == 1:
            unique_key = unique_keys[0]
            return unique_key, LIVE_CONTEXTS[unique_key]
        if len(unique_keys) > 1:
            logging.debug(f"LIVE_CONTEXTS: Ambiguous name lookup for '{clean_name}' ({unique_keys})")
            return None, None

    if conflicting_hits:
        conflict_key, conflict_ctx, conflict_name = conflicting_hits[0]
        logging.warning(
            f"LIVE_CONTEXTS: identity conflict for '{clean_name}' via key '{conflict_key}' "
            f"(cached as '{conflict_name}'). Falling back to conflicting id because no unique name match exists."
        )
        return conflict_key, conflict_ctx

    return None, None

def store_live_context(context, name=None, explicit_id=None):
    """Record an NPC's live context.

    The body is a read-modify-write across two structures: resolve, evict the
    old key, insert the new one, re-register the alias, then trim the cache.
    Run unsynchronised, two threads could interleave anywhere in there and
    leave LIVE_NAME_INDEX pointing at a key that no longer exists -- which is
    how an NPC ends up answering with somebody else's personality. The cap
    eviction below is also a next(iter(...)) over a dict another thread may be
    inserting into, which raises RuntimeError outright.
    """
    with IDENTITY_LOCK:
        return _store_live_context_locked(context, name, explicit_id)


def _store_live_context_locked(context, name=None, explicit_id=None):
    ctx_dict = _parse_context_dict(context, fallback_name=name)
    if not isinstance(ctx_dict, dict):
        return None, {}

    clean_name = _context_name(ctx_dict, name)
    faction = _context_faction(ctx_dict)
    derived_storage_id = _build_name_faction_id(clean_name, faction) if clean_name else ""
    runtime_id = str(ctx_dict.get("runtime_id") or ctx_dict.get("id") or "").strip() or None
    persistent_id = str(ctx_dict.get("persistent_id") or "").strip() or None
    storage_id = _sv()._preferred_storage_id(
        clean_name,
        faction,
        ctx_dict.get("storage_id"),
        ctx_dict.get("ID"),
        explicit_id if explicit_id and not re.fullmatch(r"-?\d+", str(explicit_id)) else None,
        uid=persistent_id,
    )
    key = str(runtime_id or explicit_id or clean_name or "")
    if not key:
        return None, {}

    key_conflict = False
    conflicting_ctx = LIVE_CONTEXTS.get(key) if key in LIVE_CONTEXTS else None
    conflicting_name = _context_name(conflicting_ctx) if conflicting_ctx else ""
    if clean_name and conflicting_name and conflicting_name != clean_name:
        live_keys = [k for k in LIVE_NAME_INDEX.get(clean_name, []) if k in LIVE_CONTEXTS]
        unique_keys = list(dict.fromkeys(live_keys))
        if len(unique_keys) == 1 and unique_keys[0] != key:
            logging.warning(
                f"LIVE_CONTEXTS: refusing to rebind key '{key}' from '{conflicting_name}' to '{clean_name}'. "
                f"Using existing name-bound key '{unique_keys[0]}' instead."
            )
            key = unique_keys[0]
            key_conflict = True

    existing_key, existing = resolve_live_context(name=clean_name, context=ctx_dict, explicit_id=explicit_id)
    merged = dict(existing or {})
    incoming = dict(ctx_dict)
    if key_conflict:
        for identity_key in ("runtime_id", "id", "persistent_id", "storage_id", "ID"):
            incoming.pop(identity_key, None)
    merged.update(incoming)

    if clean_name:
        merged["name"] = clean_name
    if runtime_id and not key_conflict:
        merged["runtime_id"] = runtime_id
        merged["id"] = runtime_id
    elif explicit_id and not merged.get("id") and not key_conflict:
        merged["id"] = explicit_id
    if _is_strong_uid(persistent_id) and not key_conflict:
        merged["persistent_id"] = persistent_id
    if storage_id and not key_conflict:
        merged["storage_id"] = storage_id
    elif derived_storage_id:
        merged["storage_id"] = derived_storage_id

    merged["_aliases"] = [clean_name] if clean_name else []

    if existing_key and existing_key != key:
        _unregister_live_aliases(existing_key, existing)
        LIVE_CONTEXTS.pop(existing_key, None)

    LIVE_CONTEXTS.pop(key, None)
    LIVE_CONTEXTS[key] = merged
    _register_live_name(key, clean_name)
    # Evict oldest entry when cache exceeds cap (prevents unbounded growth in long sessions)
    if len(LIVE_CONTEXTS) > 300:
        oldest_key = next(iter(LIVE_CONTEXTS))
        _unregister_live_aliases(oldest_key, LIVE_CONTEXTS.pop(oldest_key))

    _istate.last_npc_key = key
    _istate.last_npc_name = clean_name or key
    return key, merged

def clear_live_context_cache():
    with IDENTITY_LOCK:
        LIVE_CONTEXTS.clear()
        LIVE_NAME_INDEX.clear()
    _istate.last_npc_key = None
    _istate.last_npc_name = None
    _istate.last_direct_chat_key = None
    _istate.last_direct_chat_at = 0.0
    with _sv().AMBIENT_LOCK:
        AMBIENT_SPEAKER_LAST_AT.clear()
    with _sv().PROGRESS_LOCK:
        _sv().PROFILES_IN_PROGRESS.clear()
        # Same lock as the rest of the profile pipeline. It used to be cleared
        # under PRIORITY_LOCK while the live code path guards it with
        # PROGRESS_LOCK, so the two never actually excluded each other.
        _sv().DEFERRED_PROFILE_QUEUE.clear()
    with PRIORITY_LOCK:
        _istate.active_direct_chat_count = 0
        _istate.chat_priority_until = 0.0

def _ambient_identity_key(name=None, context=None, explicit_id=None):
    clean_name = _clean_npc_name(name)
    key, _ = resolve_live_context(name=clean_name, context=context, explicit_id=explicit_id)
    if key:
        return key
    if explicit_id and clean_name:
        return f"{clean_name}|{explicit_id}"
    return clean_name

def mark_recent_direct_chat(name, context=None, explicit_id=None):
    key = _ambient_identity_key(name=name, context=context, explicit_id=explicit_id)
    if not key:
        return
    with _sv().AMBIENT_LOCK:
        _istate.last_direct_chat_key = key
        _istate.last_direct_chat_at = time.time()

def ambient_candidate_allowed(name=None, context=None, explicit_id=None):
    if context_is_dead(_parse_context_dict(context, fallback_name=name)):
        return False

    key = _ambient_identity_key(name=name, context=context, explicit_id=explicit_id)
    if not key:
        return False

    now = time.time()
    with _sv().AMBIENT_LOCK:
        if _istate.last_direct_chat_key == key and (now - _istate.last_direct_chat_at) < AMBIENT_DIRECT_CHAT_COOLDOWN:
            return False

        last_spoke = AMBIENT_SPEAKER_LAST_AT.get(key, 0.0)
        if (now - last_spoke) < AMBIENT_SPEAKER_COOLDOWN:
            return False

    return True

def mark_ambient_speakers(names):
    now = time.time()
    with _sv().AMBIENT_LOCK:
        for speaker in names:
            if isinstance(speaker, dict):
                key = _ambient_identity_key(
                    name=speaker.get("name"),
                    context=speaker,
                    explicit_id=(
                        speaker.get("persistent_id")
                        or speaker.get("runtime_id")
                        or speaker.get("storage_id")
                        or speaker.get("id")
                    ),
                )
            elif isinstance(speaker, (tuple, list)):
                name = speaker[0] if len(speaker) > 0 else None
                explicit_id = speaker[1] if len(speaker) > 1 else None
                context = speaker[2] if len(speaker) > 2 else None
                key = _ambient_identity_key(name=name, context=context, explicit_id=explicit_id)
            else:
                key = _ambient_identity_key(name=speaker)

            if key:
                AMBIENT_SPEAKER_LAST_AT[key] = now

def begin_direct_chat():
    with PRIORITY_LOCK:
        _istate.active_direct_chat_count += 1
        _istate.chat_priority_until = time.time() + DIRECT_CHAT_GRACE_SECONDS

def end_direct_chat():
    with PRIORITY_LOCK:
        if _istate.active_direct_chat_count > 0:
            _istate.active_direct_chat_count -= 1
        _istate.chat_priority_until = time.time() + DIRECT_CHAT_GRACE_SECONDS

def direct_chat_active():
    with PRIORITY_LOCK:
        return _istate.active_direct_chat_count > 0 or time.time() < _istate.chat_priority_until

# NOTE: defer_profile_batch / drain_deferred_profile_queue /
# _launch_batch_profile_generation / flush_deferred_profile_batches used to be
# duplicated here under PRIORITY_LOCK while kenshi_llm_server.py kept its own
# copies under PROGRESS_LOCK -- both writing the same DEFERRED_PROFILE_QUEUE, so
# neither lock excluded the other and a fix applied to one copy never reached
# the other. Nothing imported these, so the server's versions are the only ones.

class _DirectChatLease:
    """Small route-scope guard so direct chat priority is always released on return/exception."""
    def __init__(self):
        self.active = False

    def begin(self):
        if not self.active:
            begin_direct_chat()
            self.active = True

    def release(self):
        if self.active:
            end_direct_chat()
            self.active = False

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass



# ─── PUBLIC API ──────────────────────────────────────────────────────────────

__all__ = [
    # Owned globals (server imports these back)
    "LIVE_CONTEXTS",
    "LIVE_NAME_INDEX",
    "PLAYER_CONTEXT",
    "SELECTED_PLAYER_CONTEXT",
    "AMBIENT_SPEAKER_LAST_AT",
    "_istate",
    "AMBIENT_DIRECT_CHAT_COOLDOWN",
    "AMBIENT_SPEAKER_COOLDOWN",
    "DIRECT_CHAT_GRACE_SECONDS",
    "SELECTED_TTL_SECONDS",
    "PRIORITY_LOCK",
    "SELECTED_LOCK",
    # Selected speaker
    "selected_speaker_enabled",
    "set_selected_player_context",
    "selected_is_fresh",
    "get_effective_player_context",
    "get_effective_player_name",
    "selected_speaker_status",
    # /speaker command
    "remember_chat_nearby",
    "list_squad_names",
    "resolve_speaker_candidate",
    "set_speaker_by_name",
    "clear_speaker_command",
    "command_speaker_active",
    # Utility
    "_build_name_faction_id",
    # Identity functions
    "_normalize_context_identity",
    "_normalized_faction_key",
    "_context_identity_summary",
    "context_is_dead",
    "_collect_target_candidates",
    "resolve_primary_target",
    "_parse_context_dict",
    "_context_name",
    "_context_faction",
    "_register_live_name",
    "_unregister_live_aliases",
    "resolve_live_context",
    "store_live_context",
    "clear_live_context_cache",
    "_ambient_identity_key",
    "mark_recent_direct_chat",
    "ambient_candidate_allowed",
    "mark_ambient_speakers",
    "begin_direct_chat",
    "end_direct_chat",
    "direct_chat_active",
    "_DirectChatLease",
]
