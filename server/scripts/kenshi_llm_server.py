# Sentient Sands - Kenshi AI Mod
# Copyright (C) 2026 Sentient Sands Team
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import copy
import os
import shutil
import atexit
import ctypes
import json
import logging
import subprocess
import signal
import socket
import requests
import re
import time
import hashlib
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
import sys
import logging.handlers
import traceback
import textwrap

# --- PATH DEFINITIONS (Bootstrap only for local imports) ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Explicitly add script dir to path for imports
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import personality_rules as personality_rules_mod
from ss_error_reporter import setup_server_error_report_logger

from configuration import (
    CAMPAIGNS_DIR,
    CHARACTERS_DIR as DEFAULT_CHARACTERS_DIR,
    GENERIC_NAMES_PATH,
    KENSHI_MOD_DIR,
    KENSHI_SERVER_DIR,
    LOCALIZATION_PATH,
    MODELS_PATH,
    NAMES_PATH,
    PROVIDERS_PATH,
    RENAMING_LIST_PATH,
    RENAMING_RULES_PATH,
    TEMPLATES_DIR,
    persist_current_settings,
    get_config_radii,
    load_settings,
    save_settings,
)
from server_runtime import (
    set_current_model_key,
    set_debug_logger,
    set_model_configs,
    set_player2_session_key,
)
from campaign_chronicle import append_major_event, build_chronicle_block, consider_event, load_chronicle, save_chronicle
from personality_rules import (
    ANIMAL_RACES,
    MACHINE_RACES,
    build_loyalty_note,
    generate_npc_traits,
    get_trait_parts,
)
from save_reader import build_world_index, cleanup_fragment_registry_files
from trade_items import load_item_aliases, normalize_trade_item_name
from ss_persistence import (
    # Utilities
    _clean_npc_name, _is_strong_uid, _safe_char_filename,
    # Storage ID
    make_storage_id, extract_id_from_context,
    # Profile decisions
    should_save_profile, is_placeholder_profile, profile_needs_upgrade,
    # Name registry
    get_used_names,
)
from ss_identity import (
    # Selected speaker (SelectedSpeaker plugin overrides the stock squad-slot-1 speaker)
    set_selected_player_context, selected_is_fresh, selected_speaker_status,
    selected_speaker_enabled,
    get_effective_player_context, get_effective_player_name,
    # /speaker command — same override, driven from chat instead of the plugin
    remember_chat_nearby, list_squad_names, set_speaker_by_name,
    clear_speaker_command, command_speaker_active,
    # Owned globals
    LIVE_CONTEXTS, LIVE_NAME_INDEX, PLAYER_CONTEXT,
    AMBIENT_SPEAKER_LAST_AT, _istate,
    AMBIENT_DIRECT_CHAT_COOLDOWN, AMBIENT_SPEAKER_COOLDOWN,
    DIRECT_CHAT_GRACE_SECONDS, PRIORITY_LOCK,
    # Utility
    _build_name_faction_id,
    # Identity functions
    _normalize_context_identity, _normalized_faction_key,
    _context_identity_summary, context_is_dead, _collect_target_candidates,
    resolve_primary_target, _parse_context_dict,
    _context_name, _context_faction,
    _register_live_name, _unregister_live_aliases,
    resolve_live_context, store_live_context, clear_live_context_cache,
    _ambient_identity_key, mark_recent_direct_chat,
    get_persona_category,
    ambient_candidate_allowed, mark_ambient_speakers,
    begin_direct_chat, end_direct_chat, direct_chat_active,
    _DirectChatLease,
)
from ss_prompt import (
    build_detailed_context_string,
    load_prompt_component,
    format_player_status,
    format_player_inventory,
    fetch_dynamic_lore,
    build_events_block,
    build_system_prompt,
)
from ss_llm import (
    sanitize_llm_text,
    robust_json_parse,
    call_llm,
)
from ss_character_gateway import CharacterGateway

# added by Pineaxe v04 - Kayak bridge (import happens after logging is configured)
kayak = None
kayak_hub = None
character_gateway = CharacterGateway()  # Phase 1: Kayak-only character I/O
KAYAK_ENABLED = False
_KAYAK_LAST_RETRY = 0.0
_KAYAK_PROCESS = None
_KAYAK_STARTED_BY_SERVER = False

# Character creation handler for Kayak integration
try:
    from ss_character_creation_handler import (
        create_from_profile,
        create_batch,
        sync_registry_from_kayak,
        get_npc_id,
    )
    _HAVE_CHARACTER_HANDLER = True
except ImportError:
    _HAVE_CHARACTER_HANDLER = False
    logging.debug("ss_character_creation_handler not available (character creation will use native system)")

try:
    from simplify_global_events import (
        extract_raw_events_from_text as _sge_parse,
        simplify_events_to_consolidated_text as _sge_compress,
        extra_token_reduction as _sge_reduce,
    )
    _HAVE_COMPRESSOR = True
except ImportError:
    _HAVE_COMPRESSOR = False
    logging.warning("simplify_global_events.py not found — narrative synthesis will use raw events.")

# added by AntiGravity - kill old servers BEFORE we start any child processes (like Kayak)
def kill_old_servers():
    """Cleanup any orphaned SentientSands servers on port 5000 before we begin.
    
    NOTE: We deliberately do NOT kill port 5001 (Kayak). Kayak is a long-running
    daemon that should survive across SentientSands server restarts. Killing it
    caused repeated connection failures every time the game reloaded.
    """
    import subprocess
    import time
    try:
        # Windows specific: find processes on port 5000/5001
        result = subprocess.run(
            ['netstat', '-aon'], capture_output=True, text=True, shell=False, timeout=5
        )
        _seen = set()
        for line in result.stdout.splitlines():
            if ':5000' in line and 'LISTENING' in line:
                parts = line.strip().split()
                if not parts: continue
                try:
                    pid = int(parts[-1])
                except Exception: continue
                if pid in _seen: continue
                _seen.add(pid)
                if pid > 0 and pid != os.getpid():
                    logging.info(f"Port cleanup: Terminating old SentientSands process {pid} on {parts[1]}...")
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, shell=False, timeout=5)
        # Sockets take a moment to release
        time.sleep(0.5)
    except Exception as e:
        if 'logging' in globals():
            logging.warning(f"Port cleanup fallback: {e}")

kill_old_servers()

NAMES_CONFIG = {}
GENERIC_CONFIG = {}
LOCALIZATION_CONFIG = {}
RENAMING_LIST: set = set()    # exact-match title-only names eligible for renaming
RENAMING_RULES: dict = {}     # mode, preserve_existing_named, match
CURRENT_MODEL_KEY = "player2-default" # Default
ACTIVE_CAMPAIGN = "Default"      # Default

CHARACTERS_DIR = DEFAULT_CHARACTERS_DIR # Initial fallback; campaign switches overwrite this
CURRENT_CAMPAIGN = "Default" # Global track for UI
LAST_GENERATE_TIME = 0 # Track last rumor timestamp
GLOBAL_SYNTHESIS_INTERVAL = 60 # Default minutes
DIALOGUE_HISTORY_LIMIT = 45    # Max dialogue lines kept per NPC (configurable via INI)

EVENT_HISTORY = []
EVENT_HISTORY_SET = set()  # Parallel set for O(1) dedup lookup
SHOP_STOCK = {}
SHOP_STOCK_MTIME = 0.0
PROFILES_IN_PROGRESS = set()
PROGRESS_LOCK = threading.Lock()
PENDING_PROFILES = {}
PENDING_LOCK = threading.Lock()
# LIVE_CONTEXTS, LIVE_NAME_INDEX, PLAYER_CONTEXT live in ss_identity — imported above
# LAST_NPC_NAME, LAST_NPC_KEY, LAST_DIRECT_CHAT_KEY etc live in ss_identity._istate — imported above
PLAYER2_SESSION_KEY = None
EVENT_THROTTLE = {}
THROTTLE_LOCK = threading.Lock()
LAST_STATE_LOG = {} # { "NPCName|etype": "last_msg" }
STATE_LOCK = threading.Lock()
AMBIENT_LOCK = threading.Lock()
# AMBIENT_SPEAKER_LAST_AT, AMBIENT_DIRECT_CHAT_COOLDOWN, AMBIENT_SPEAKER_COOLDOWN live in ss_identity
# PRIORITY_LOCK, ACTIVE_DIRECT_CHAT_COUNT, CHAT_PRIORITY_UNTIL, DIRECT_CHAT_GRACE_SECONDS live in ss_identity
DEFERRED_PROFILE_QUEUE = {}
SYNTHESIS_STATUS = {"elapsed": 0, "interval": 60}
AUTO_SYNTHESIS_PENDING = False
AUTO_SYNTHESIS_PENDING_AT = 0.0
_RUMORS_CACHE: list = []
_RUMORS_CACHE_MTIME: float = 0.0
_COMPONENT_CACHE: dict = {}   # { full_filepath: (mtime, content) } — avoids repeated template disk reads
WORLD_INDEX: dict = {}        # { npc_name: [platoon, ...] } — populated by update_world_index()
ORIGINAL_NAME_HINTS = {}      # runtime-only: campaign/name-or-id -> true pre-mod name
ORIGINAL_NAME_HINTS_LOCK = threading.Lock()
RENAMING_LIST_FILE_LOCK = threading.Lock()
SPECIES_SCAFFOLD_LOCK = threading.Lock()
PERSONA_RACES_FILE_LOCK = threading.Lock()


def _original_name_hint_keys(name="", entity_id="", campaign=None):
    active_campaign = str(campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    keys = []
    clean_id = str(entity_id or "").strip()
    clean_name = _clean_npc_name(name) if name else ""
    if clean_id:
        keys.append(f"{active_campaign}|id|{clean_id}")
    if clean_name:
        keys.append(f"{active_campaign}|name|{clean_name.lower()}")
    return keys


def _store_original_name_hint(original_name, names=None, entity_ids=None, campaign=None):
    original = str(original_name or "").strip()
    if not original:
        return

    keys = set()
    for entity_id in (entity_ids or []):
        keys.update(_original_name_hint_keys(entity_id=entity_id, campaign=campaign))
    for name in (names or []):
        keys.update(_original_name_hint_keys(name=name, campaign=campaign))
    if not keys:
        return

    entry = {"original_name": original, "updated_at": time.time()}
    with ORIGINAL_NAME_HINTS_LOCK:
        for key in keys:
            ORIGINAL_NAME_HINTS[key] = dict(entry)


def _lookup_original_name_hint(name="", entity_id="", campaign=None):
    candidate_keys = []
    candidate_keys.extend(_original_name_hint_keys(entity_id=entity_id, campaign=campaign))
    candidate_keys.extend(_original_name_hint_keys(name=name, campaign=campaign))

    with ORIGINAL_NAME_HINTS_LOCK:
        for key in candidate_keys:
            entry = ORIGINAL_NAME_HINTS.get(key) or {}
            original = str(entry.get("original_name") or "").strip()
            if original:
                return original
    return ""


def _sanitize_db_list_value(value):
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return ""
    if text.startswith("#"):
        return ""
    return text


def _normalize_db_species_category(value):
    text = str(value or "").strip().lower()
    mapping = {
        "animal": "animals",
        "animals": "animals",
        "feral": "ferals",
        "ferals": "ferals",
        "machine": "machines",
        "machines": "machines",
        "sapient": "sapients",
        "sapients": "sapients",
    }
    return mapping.get(text, "")


def _safe_species_folder_name(race):
    text = str(race or "").strip()
    if not text:
        return ""
    text = re.sub(r"[^\w\s\-]", "", text)
    return re.sub(r"\s+", "_", text).strip("_")


def _normalize_persona_category_bucket(value):
    text = str(value or "").strip().lower()
    mapping = {
        "animal": "animals",
        "machine": "machines",
        "feral": "ferals",
        "sapient": "sapients",
    }
    return mapping.get(text, "")


def _kayak_species_root(scope, campaign=None):
    parts = [KENSHI_MOD_DIR, "Kayak", "KayakDB"]
    if scope == "template":
        parts.append("Template")
    else:
        parts.extend(["Campaigns", campaign or ACTIVE_CAMPAIGN or "Default"])
    parts.append("species")
    return os.path.abspath(os.path.join(*parts))


def _resolve_db_species_target(target_npc="", target_npc_id=None, context=None):
    clean_target = _clean_npc_name(target_npc)
    ctx_dict = _parse_context_dict(context, fallback_name=clean_target)
    _, live_ctx = resolve_live_context(
        name=clean_target,
        context=ctx_dict,
        explicit_id=target_npc_id,
    )

    display_name = (
        _context_name(ctx_dict, clean_target)
        or _context_name(live_ctx, clean_target)
        or str(target_npc or "").strip()
        or "NPC"
    )
    race = str(
        ctx_dict.get("race")
        or ctx_dict.get("Race")
        or live_ctx.get("race")
        or live_ctx.get("Race")
        or ""
    ).strip()
    kayak_fields = {}
    if KAYAK_ENABLED and kayak and hasattr(kayak, "get_npc_fields"):
        try:
            kayak_fields = kayak.get_npc_fields(clean_target, target_npc_id, ACTIVE_CAMPAIGN) or {}
        except Exception as err:
            logging.warning(f"DB CMD: species lookup failed ({err})")
            kayak_fields = {}
    if not race:
        race = str(kayak_fields.get("race") or "").strip()
    if not display_name:
        display_name = str(kayak_fields.get("display_name") or clean_target or "NPC").strip() or "NPC"

    return {
        "display_name": display_name,
        "race": race,
        "faction": str(
            ctx_dict.get("faction")
            or ctx_dict.get("Faction")
            or live_ctx.get("faction")
            or live_ctx.get("Faction")
            or kayak_fields.get("faction")
            or kayak_fields.get("origin_faction")
            or ""
        ).strip(),
        "persona_category": str(
            ctx_dict.get("persona_category")
            or ctx_dict.get("_persona_category")
            or live_ctx.get("persona_category")
            or live_ctx.get("_persona_category")
            or kayak_fields.get("persona_category")
            or ""
        ).strip(),
        "fields": kayak_fields,
    }


def _build_species_placeholder_text(race, category, prompt_group):
    race_text = str(race or "").strip() or "Unknown Race"
    group_text = str(prompt_group or "").strip().lower() or "prompt"
    category_text = str(category or "").strip().lower() or "species"
    return (
        f"SPECIES OVERLAY PLACEHOLDER: {race_text}\n\n"
        f"- No species-specific {group_text} guidance has been written yet.\n"
        f"- Follow the base {category_text} {group_text} rules for this race until this file is customized.\n"
    )


def _find_existing_species_category(race, campaign=None):
    species_folder = _safe_species_folder_name(race)
    if not species_folder:
        return ""
    active_campaign = str(campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    for scope_name, root in (
        ("campaign", _kayak_species_root("campaign", active_campaign)),
        ("template", _kayak_species_root("template")),
    ):
        for bucket in ("animals", "ferals", "machines", "sapients"):
            bucket_dir = os.path.join(root, bucket, species_folder)
            if os.path.isdir(bucket_dir):
                return bucket
    return ""


def _resolve_db_species_bucket(target_info, source="db_species"):
    if not isinstance(target_info, dict):
        target_info = {}
    explicit_bucket = _normalize_persona_category_bucket(target_info.get("persona_category"))
    if explicit_bucket:
        return explicit_bucket
    existing_bucket = _find_existing_species_category(target_info.get("race"), ACTIVE_CAMPAIGN)
    if existing_bucket:
        return existing_bucket
    fallback_bucket = _normalize_persona_category_bucket(
        get_persona_category(
            target_info.get("race"),
            target_info.get("faction"),
            name=target_info.get("display_name"),
            source=source,
        )
    )
    return fallback_bucket


def _ensure_species_scaffold(race, category, campaign=None):
    clean_race = str(race or "").strip()
    bucket = _normalize_db_species_category(category)
    species_folder = _safe_species_folder_name(clean_race)
    active_campaign = str(campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    if not clean_race or not bucket or not species_folder:
        raise ValueError("Race or species category is invalid.")

    prompt_groups = ("biography", "chat")
    roots = [
        ("campaign", _kayak_species_root("campaign", active_campaign)),
        ("template", _kayak_species_root("template")),
    ]
    created_files = 0
    created_dirs = 0
    touched_roots = []

    with SPECIES_SCAFFOLD_LOCK:
        for scope_name, root in roots:
            if scope_name not in touched_roots:
                touched_roots.append(scope_name)
            for prompt_group in prompt_groups:
                prompt_dir = os.path.join(root, bucket, species_folder, prompt_group)
                if not os.path.isdir(prompt_dir):
                    os.makedirs(prompt_dir, exist_ok=True)
                    created_dirs += 1
                file_path = os.path.join(prompt_dir, "1_rules.txt")
                if not os.path.exists(file_path):
                    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(_build_species_placeholder_text(clean_race, bucket, prompt_group))
                    created_files += 1

    return {
        "status": "created" if (created_dirs or created_files) else "exists",
        "campaign": active_campaign,
        "category": bucket,
        "race": clean_race,
        "species_folder": species_folder,
        "created_dirs": created_dirs,
        "created_files": created_files,
        "scopes": touched_roots,
    }


def _sanitize_db_species_rule(value):
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return ""
    return text


def _persona_races_registry_path():
    return str(
        getattr(personality_rules_mod, "PERSONA_RACES_PATH", "") or
        os.path.join(KENSHI_MOD_DIR, "config", "persona_races.json")
    ).strip()


def _persona_races_registry_defaults():
    return {
        "animal_races": list(getattr(personality_rules_mod, "ANIMAL_RACES", []) or []),
        "machine_races": list(getattr(personality_rules_mod, "MACHINE_RACES", []) or []),
        "sapient_races": list(getattr(personality_rules_mod, "SAPIENT_RACES", []) or []),
        "feral_race_keywords": list(getattr(personality_rules_mod, "FERAL_RACE_KEYWORDS", []) or []),
        "feral_faction_keywords": list(getattr(personality_rules_mod, "FERAL_FACTION_KEYWORDS", []) or []),
    }


def _append_unique_case_insensitive(items, value):
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    for existing in items:
        if str(existing or "").strip().lower() == lowered:
            return False
    items.append(text)
    return True


def _add_race_to_persona_registry(race, species_category):
    clean_race = str(race or "").strip()
    bucket = str(species_category or "").strip().lower()
    if not clean_race or bucket not in {"animals", "machines", "sapients", "ferals"}:
        raise ValueError("Race or species category is invalid.")

    target_map = {
        "animals": ["animal_races"],
        "machines": ["machine_races"],
        "sapients": ["sapient_races"],
        "ferals": ["feral_race_keywords", "feral_faction_keywords"],
    }
    registry_path = _persona_races_registry_path()
    changed_keys = []

    with PERSONA_RACES_FILE_LOCK:
        defaults = _persona_races_registry_defaults()
        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        raw = {}
        if os.path.exists(registry_path):
            try:
                with open(registry_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = dict(loaded)
            except Exception as exc:
                logging.warning(f"DB CMD: failed to load persona_races.json for write ({exc}); rebuilding from defaults")

        registry = {}
        for key, fallback in defaults.items():
            source = raw.get(key, fallback)
            values = []
            for item in source if isinstance(source, list) else fallback:
                text = str(item or "").strip()
                if not text:
                    continue
                if not any(existing.lower() == text.lower() for existing in values):
                    values.append(text)
            registry[key] = values

        for key in target_map[bucket]:
            if _append_unique_case_insensitive(registry[key], clean_race):
                changed_keys.append(key)

        with open(registry_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
            f.write("\n")

        personality_rules_mod.reload_persona_race_registry(registry_path)

    return {
        "status": "updated" if changed_keys else "exists",
        "path": registry_path,
        "race": clean_race,
        "category": bucket,
        "keys": changed_keys or target_map[bucket],
    }


def _invalidate_character_cache_aliases(*names, persistent_id=None):
    seen = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            character_gateway.invalidate(name, persistent_id)
        except Exception:
            pass
        try:
            character_gateway.invalidate(name, None)
        except Exception:
            pass


def _append_species_rule(race, category, prompt_group, rule_text, campaign=None):
    clean_rule = _sanitize_db_species_rule(rule_text)
    if not clean_rule:
        raise ValueError("Rule text is invalid.")

    scaffold = _ensure_species_scaffold(race, category, campaign)
    clean_race = str(scaffold.get("race") or race or "").strip()
    bucket = str(scaffold.get("category") or category or "").strip()
    species_folder = str(scaffold.get("species_folder") or _safe_species_folder_name(clean_race)).strip()
    active_campaign = str(scaffold.get("campaign") or campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    group = str(prompt_group or "").strip().lower()
    if group not in ("biography", "chat"):
        raise ValueError("Prompt group is invalid.")

    entry = f"- {clean_rule}"
    appended_files = 0
    for root in (
        _kayak_species_root("campaign", active_campaign),
        _kayak_species_root("template"),
    ):
        file_path = os.path.join(root, bucket, species_folder, group, "1_rules.txt")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                existing_text = f.read()
        else:
            existing_text = ""

        placeholder_text = _build_species_placeholder_text(clean_race, bucket, group)
        if existing_text.strip() == placeholder_text.strip():
            lines = []
        else:
            lines = existing_text.splitlines()

        while lines and not str(lines[-1]).strip():
            lines.pop()
        lines.append(entry)

        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        appended_files += 1

    return {
        "status": "ok",
        "campaign": active_campaign,
        "category": bucket,
        "race": clean_race,
        "species_folder": species_folder,
        "prompt_group": group,
        "entry": entry,
        "files": appended_files,
    }


def _resolve_db_title_target(target_npc="", target_npc_id=None, context=None):
    clean_target = _clean_npc_name(target_npc)
    ctx_dict = _parse_context_dict(context, fallback_name=clean_target)
    _, live_ctx = resolve_live_context(
        name=clean_target,
        context=ctx_dict,
        explicit_id=target_npc_id,
    )

    faction = _context_faction(ctx_dict) or _context_faction(live_ctx)
    display_name = (
        _context_name(ctx_dict, clean_target)
        or _context_name(live_ctx, clean_target)
        or str(target_npc or "").strip()
        or "NPC"
    )

    kayak_fields = {}
    if KAYAK_ENABLED and kayak and hasattr(kayak, "get_npc_fields"):
        try:
            kayak_fields = kayak.get_npc_fields(clean_target, target_npc_id, ACTIVE_CAMPAIGN) or {}
        except Exception as err:
            logging.warning(f"DB CMD: title lookup failed ({err})")
            kayak_fields = {}

    if not faction:
        faction = str(
            kayak_fields.get("faction")
            or kayak_fields.get("origin_faction")
            or ""
        ).strip()
    if not display_name:
        display_name = str(kayak_fields.get("display_name") or clean_target or "NPC").strip() or "NPC"

    return {
        "display_name": display_name,
        "faction": faction,
        "fields": kayak_fields,
    }


def _add_title_to_renaming_list(title, faction):
    clean_title = _sanitize_db_list_value(title)
    clean_faction = _sanitize_db_list_value(faction)
    if not clean_title or not clean_faction:
        raise ValueError("Title or faction is invalid.")

    with RENAMING_LIST_FILE_LOCK:
        if os.path.exists(RENAMING_LIST_PATH):
            with open(RENAMING_LIST_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        else:
            lines = []

        current_section = ""
        existing_section = ""
        for raw_line in lines:
            stripped = raw_line.strip()
            if stripped.startswith("# "):
                current_section = stripped[2:].strip()
                continue
            if stripped.startswith("#") or not stripped:
                continue
            if stripped == clean_title:
                existing_section = current_section
                break

        if existing_section or clean_title in {str(line).strip() for line in lines if str(line).strip() and not str(line).strip().startswith("#")}:
            return {
                "status": "exists",
                "section": existing_section,
                "title": clean_title,
                "faction": clean_faction,
            }

        header_line = f"# {clean_faction}"
        header_index = None
        for idx, raw_line in enumerate(lines):
            if raw_line.strip() == header_line:
                header_index = idx
                break

        if header_index is not None:
            insert_at = header_index + 1
            while insert_at < len(lines):
                stripped = lines[insert_at].strip()
                if not stripped or stripped.startswith("#"):
                    break
                insert_at += 1
            lines.insert(insert_at, clean_title)
            status = "added"
        else:
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines.append("")
            lines.append(header_line)
            lines.append(clean_title)
            status = "created_section"

        with open(RENAMING_LIST_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

        load_renaming_config()
        push_generic_names_to_dll()

    return {
        "status": status,
        "section": clean_faction,
        "title": clean_title,
        "faction": clean_faction,
    }

set_current_model_key(CURRENT_MODEL_KEY)
set_player2_session_key(PLAYER2_SESSION_KEY)



# --- CORE GLOBALS & CONFIG PATHS ---
LLM_SESSION = requests.Session()

# --- LORE DATABASE CACHE ---
LORE_DATABASE = []

# Type-based grouping for fetch_dynamic_lore — controls prompt section order,
# headers, and per-type character budgets.
_LORE_TYPE_ORDER   = ["global", "race", "faction", "theology", "region"]
_LORE_TYPE_HEADERS = {
    "global":   "WORLD CONTEXT",
    "race":     "RACE",
    "faction":  "FACTION",
    "theology": "RELIGION",
    "region":   "REGION",
}
_LORE_TYPE_BUDGETS = {
    "global":   650,   # world overview + commerce — always injected
    "race":     400,   # race biology/psychology — usually 1 chunk
    "faction": 1100,   # major faction + relevant minor factions (2 major chunks ~500 chars each)
    "theology": 400,   # faith/religion context
    "region":   500,   # environmental/location hazards (chunks are 330-450 chars)
}

def initialize_lore_database():
    """Parses the JSON lore file into system memory on server startup."""
    global LORE_DATABASE
    lore_db_path = os.path.join(TEMPLATES_DIR, "World_lore.json")
    try:
        if os.path.exists(lore_db_path):
            # utf-8-sig strips a Windows BOM if present, identical to utf-8 otherwise
            with open(lore_db_path, "r", encoding="utf-8-sig") as f:
                LORE_DATABASE = json.load(f)
            if not isinstance(LORE_DATABASE, list):
                logging.error(
                    f"SYSTEM: {lore_db_path} parsed as {type(LORE_DATABASE).__name__}, expected list. Resetting."
                )
                LORE_DATABASE = []
            else:
                logging.info(f"SYSTEM: Successfully cached {len(LORE_DATABASE)} lore chunks into memory.")
                # Validate each chunk has required fields and a recognized type
                _valid_types = set(_LORE_TYPE_ORDER)
                _required_keys = {"id", "type", "tags", "content"}
                _warnings = 0
                for chunk in LORE_DATABASE:
                    missing = _required_keys - chunk.keys()
                    if missing:
                        logging.warning(f"LORE VALIDATION: chunk '{chunk.get('id','?')}' missing fields: {missing}")
                        _warnings += 1
                    elif chunk["type"] not in _valid_types:
                        logging.warning(f"LORE VALIDATION: chunk '{chunk['id']}' has unknown type '{chunk['type']}' — will be ignored by fetch_dynamic_lore")
                        _warnings += 1
                if _warnings == 0:
                    logging.info(f"SYSTEM: Lore validation passed — all {len(LORE_DATABASE)} chunks are well-formed.")
                else:
                    logging.warning(f"SYSTEM: Lore validation found {_warnings} issue(s). Check warnings above.")
        else:
            logging.warning(f"SYSTEM: {lore_db_path} not found. LLM will use static fallback text.")
    except Exception as e:
        logging.error(f"SYSTEM: Critical error parsing {lore_db_path}: {e}")
        LORE_DATABASE = []

# --- FACTION METADATA & LORE ENHANCEMENTS ---
FACTION_METADATA = {
    "The Holy Nation": {
        "Leader": "Holy Lord Phoenix LXII",
        "Desc": "A xenophobic, religious group worshipping Okran. They value human purity and despise Skeletons and non-humans."
    },
    "Holy Nation Outlaws": {
        "Leader": "None",
        "Desc": "Criminals, heretics, and exiles cast out by the Holy Nation. They survive as bandits on the fringes of Okranite territory, hated by their former brethren."
    },
    "United Cities": {
        "Leader": "Emperor Tengu",
        "Desc": "A vast, corrupt empire where wealth is law. They rely on slavery and the Traders Guild."
    },
    "Shek Kingdom": {
        "Leader": "Esata the Stone Golem",
        "Desc": "A warrior race obsessed with honor and strength, currently attempting to move away from suicidal traditions."
    },
    "Traders Guild": {
        "Leader": "Longen",
        "Desc": "A powerful commercial alliance that controls much of the world's economy through slave labor and trade."
    },
    "Anti-Slavers": {
        "Leader": "Tinfist",
        "Desc": "A group of martial-artist Skeletons and humans dedicated to the total abolition of slavery."
    },
    "Second Empire": {
        "Leader": "Mad Cat-Lon",
        "Desc": "The fallen remains of a once-great robotic empire, now reduced to madness and decay in the Ashlands."
    },
    "Second Empire Exiles": {
        "Leader": "None (Scattered Skeletons)",
        "Desc": "Rogue Skeleton remnants of the Second Empire who rejected Cat-Lon's madness. They wander the Ashlands and Greyshelf in damaged patrols."
    },
    "Western Hive": {
        "Leader": "The Hive Queen",
        "Desc": "A reclusive insectoid society focused on industrious trade and pheromone-driven loyalty to their Queen."
    },
    "Southern Hive": {
        "Leader": "The Queen of the South",
        "Desc": "A territorial and aggressive Hive variant that views all outsiders as food for their King."
    },
    "Dark Hive": {
        "Leader": "Unknown (Hive Prince)",
        "Desc": "A hostile Hive variant in Venge and the Southern Hive areas. Violent, territorial, and openly attack outsiders on sight."
    },
    "Flotsam Ninjas": {
        "Leader": "Moll",
        "Desc": "Fugitive women who escaped the Holy Nation and now wage a guerrilla war against Lord Phoenix."
    },
    "Shinobi Thieves": {
        "Leader": "The Big Boss",
        "Desc": "A global network of spies, smugglers, and fences with safehouses in most major cities."
    },
    "Nameless": {
        "Leader": "The Player",
        "Desc": "A rising group of wanderers who are beginning to make their mark on the world."
    },
    "Deadcat": {
        "Leader": "None (Scattered remnant)",
        "Desc": "Survivors of a once-proud fishing nation, now largely wiped out by Cannibals."
    },
    "Mongrel": {
        "Leader": "None (The High Shack)",
        "Desc": "A haven for outcasts and 'Fog-free' exiles in the heart of the Fog Islands."
    },
    "Red Sabres": {
        "Leader": "Red Sabre Leader",
        "Desc": "Desperate bandits and deserters who raid travelers in the Swamp."
    },
    "Swamp Ninjas": {
        "Leader": "Shade",
        "Desc": "A skilled group of ninja outlaws specializing in swamp combat and drug running."
    },
    "Reavers": {
        "Leader": "None (Warlords)",
        "Desc": "Violent raiders based in the Hook region. Fanatical fighters who attack caravans and settlements for sport and plunder."
    },
    "Berserkers": {
        "Leader": "None",
        "Desc": "Wild warriors in the northern territories who fight with reckless abandon, viewing death in battle as the highest honor."
    },
    "Cannibals": {
        "Leader": "None (Tribal)",
        "Desc": "Deranged humans in the Cannibal Plains who hunt, kill, and eat anyone who enters their territory."
    },
    "Dust Bandits": {
        "Leader": "None",
        "Desc": "Desperate, poorly armed bandits who prey on lone travelers in the Border Zone and Great Desert."
    },
    "Band of Bones": {
        "Leader": "None (Tribal chief)",
        "Desc": "A brutal bandit tribe in Stenn Desert, more organized and dangerous than typical Dust Bandits."
    },
    "Fogmen": {
        "Leader": "None (Hive Prince)",
        "Desc": "Degenerate Hivers in the Fog Islands who drag unconscious victims to fog-covered poles as offerings."
    },
    "Skin Bandits": {
        "Leader": "Savant",
        "Desc": "Deranged Skeletons who kidnap people and wear their skin, operating from Skinhouse headquarters."
    },
    "Slave Traders": {
        "Leader": "None",
        "Desc": "Licensed slavers operating under United Cities law, running slave camps and farms across the empire."
    },
    "Manhunters": {
        "Leader": "None",
        "Desc": "Licensed bounty hunters who track escaped slaves across UC territory for profit."
    },
    "Tech Hunters": {
        "Leader": "None (Council)",
        "Desc": "Scholars and explorers who scavenge Ancient ruins for pre-collapse technology and knowledge."
    },
    "Kral's Chosen": {
        "Leader": "Kral",
        "Desc": "A rogue Shek warband that rejected Esata's reforms and continue the old ways of suicidal honor combat."
    },
    "Nomads": {
        "Leader": "None",
        "Desc": "Peaceful wandering traders who travel between settlements selling goods. They avoid conflict when possible."
    },
    "Black Dragon Ninjas": {
        "Leader": "None",
        "Desc": "An elite ninja faction operating in the shadows. Highly skilled assassins and thieves."
    },
    "Blackshifters": {
        "Leader": "None",
        "Desc": "A Swamp gang based in Shark. One of five factions competing for territory in the city, known for using Moon Cleavers."
    },
    "Hounds": {
        "Leader": "Big Grim",
        "Desc": "The dominant gang in Shark who nominally control the city. Swamp-hardened criminals and thugs."
    },
    "Stone Rats": {
        "Leader": "None",
        "Desc": "A Swamp gang operating in Shark, competing with the Hounds, Blackshifters, Twinblades, and Grayflayers."
    },
    "Twinblades": {
        "Leader": "None",
        "Desc": "A Swamp gang in Shark known for their dual-wielding fighting style."
    },
    "Grayflayers": {
        "Leader": "None",
        "Desc": "A brutal Swamp gang in Shark with a reputation for cruelty."
    }
}

def get_faction_info(faction_name):
    """Returns a formatted string describing the faction and its leader."""
    if not faction_name or faction_name == "Unknown":
        return "Unknown Faction (Remnant or Drifter)"
    
    # Normalization for Player and various squad names
    clean_name = faction_name
    if "Player" in faction_name or faction_name == "Nameless":
        clean_name = "Nameless"
    
    meta = FACTION_METADATA.get(clean_name)
    if not meta:
        # Case-insensitive exact match first
        clean_lower = clean_name.lower()
        for k, v in FACTION_METADATA.items():
            if k.lower() == clean_lower:
                meta = v
                break
    if not meta:
        # Also try with "The " prefix (DLL sometimes drops articles)
        for k, v in FACTION_METADATA.items():
            if k.lower() == "the " + clean_name.lower():
                meta = v
                break
    if not meta:
        # Fuzzy fallback: prefer the CLOSEST-LENGTH match.
        # "Holy Nation" should match "The Holy Nation" (diff=3) not
        # "Holy Nation Outlaws" (diff=9).
        clean_lower = clean_name.lower()
        best_match = None
        best_diff = 9999
        for k, v in FACTION_METADATA.items():
            k_lower = k.lower()
            if k_lower in clean_lower or clean_lower in k_lower:
                diff = abs(len(k) - len(clean_name))
                if diff < best_diff:
                    best_match = v
                    best_diff = diff
        meta = best_match
    
    if meta:
        leader_part = f" (Led by {meta['Leader']})" if meta.get('Leader') else ""
        return f"{clean_name}{leader_part}: {meta['Desc']}"
    
    return f"{faction_name}: A minor or specialized group in the wasteland."

# sanitize_llm_text and robust_json_parse live in ss_llm — imported above

# Setup logging
_log_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
_log_dir = os.path.join(SCRIPT_DIR, "..", "logs")
if not os.path.exists(_log_dir):
    try:
        os.makedirs(_log_dir)
    except:
        pass

# 1. Main Server Log (Circular/Limited)
_log_file = os.path.join(_log_dir, "server.log")
# 2. Comprehensive Debug Log (Last ~500 entries)
_debug_file = os.path.join(KENSHI_SERVER_DIR, "debug.log")

try:
    # server.log: 512KB limit, 3 backups
    _file_handler = logging.handlers.RotatingFileHandler(_log_file, maxBytes=512*1024, backupCount=3, encoding='utf-8')
    _file_handler.setFormatter(_log_fmt)
    
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(_log_fmt)
    
    # debug.log: 1MB limit, 1 backup
    _debug_handler = logging.handlers.RotatingFileHandler(_debug_file, maxBytes=1024*1024, backupCount=1, encoding='utf-8')
    _debug_handler.setFormatter(_log_fmt)
    _debug_handler.setLevel(logging.DEBUG)

    # Global config
    logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler, _debug_handler])
    
    # Specialized logger for high-volume telemetry (prompts, raw data)
    # This prevents server.log from becoming a wall of text.
    debug_logger = logging.getLogger('kenshi_debug')
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.addHandler(_debug_handler)
    debug_logger.propagate = False # Do not double-log to root handlers

except Exception as e:
    # Fallback to stream only if file handler fails
    logging.basicConfig(level=logging.INFO)
    logging.error(f"Failed to initialize file logging: {e}")
    debug_logger = logging.getLogger('kenshi_debug')  # fallback: no file handler

set_debug_logger(debug_logger)

# Filtered support log: warnings/errors only, easy for players to send.
try:
    ss_error_report_log = setup_server_error_report_logger(_log_dir, debug_logger)
    logging.info(f"SentientSands error report log: {ss_error_report_log}")
except Exception as e:
    logging.warning(f"Failed to initialize SentientSands error report log: {e}")

# Silence noise
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# initialize_lore_database()  # DEPRECATED in KV05 - Lore is now handled by Kayak RAG.

# added by Pineaxe v04 - Kayak bridge import (here so errors appear in log file)
# _kayak_try_connect() is defined here (after logging) and called at startup below,
# then again lazily from /chat every 60s if Kayak is unreachable.
def _kayak_try_connect():
    global kayak, kayak_hub, KAYAK_ENABLED, _KAYAK_LAST_RETRY, _KAYAK_PROCESS, _KAYAK_STARTED_BY_SERVER
    import time
    _KAYAK_LAST_RETRY = time.monotonic()
    try:
        _KAYAK_ROOT = os.path.join(SCRIPT_DIR, '..', '..', 'Kayak')
        if _KAYAK_ROOT not in sys.path:
            sys.path.insert(0, _KAYAK_ROOT)
        from bridges.sentient_sands import SentientSandsBridge
        if kayak is None:
            kayak = SentientSandsBridge(kayak_url='http://127.0.0.1:5001')
        # Auto-start Kayak if not already running
        if not kayak.is_alive(force=True):
            _kayak_py = os.path.join(_KAYAK_ROOT, 'kayak_server.py')
            _python   = sys.executable
            if os.path.isfile(_kayak_py):
                logging.info('KAYAK: Server not running — attempting auto-start...')
                try:
                    import subprocess
                    _KAYAK_PROCESS = subprocess.Popen(
                        [_python, _kayak_py],
                        cwd=_KAYAK_ROOT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    )
                    _KAYAK_STARTED_BY_SERVER = True
                    logging.info(f'KAYAK: Auto-start launched (PID {_KAYAK_PROCESS.pid}).')
                    deadline = time.monotonic() + 15.0
                    while time.monotonic() < deadline:
                        if _KAYAK_PROCESS.poll() is not None:
                            logging.warning(
                                f'KAYAK: Auto-start process exited early with code {_KAYAK_PROCESS.returncode}.'
                            )
                            break
                        if kayak.is_alive(force=True):
                            break
                        time.sleep(0.5)
                except Exception as _ae:
                    logging.warning(f'KAYAK: Auto-start failed ({_ae})')
        KAYAK_ENABLED = kayak.is_alive(force=True)
        if KAYAK_ENABLED:
            kayak_hub = kayak
            character_gateway.set_bridge(kayak)
            character_gateway.invalidate()  # fresh start on reconnect
            try:
                _ss_campaign_dir = os.path.join(CAMPAIGNS_DIR, ACTIVE_CAMPAIGN)
                kayak.on_save_loaded(ACTIVE_CAMPAIGN, ss_campaign_dir=_ss_campaign_dir)
                if _HAVE_CHARACTER_HANDLER:
                    sync_registry_from_kayak(campaign=ACTIVE_CAMPAIGN, kayak_bridge=kayak)
            except Exception as _k_sync_e:
                logging.warning(f"KAYAK: Post-connect campaign sync failed ({_k_sync_e})")
            logging.info('KAYAK: Bridge connected and ready on port 5001.')
        else:
            kayak_hub = None
            character_gateway.set_bridge(None)
            logging.warning('KAYAK: Server not reachable on port 5001 - falling back. Start START_KAYAK.bat or it will auto-start next retry.')
    except Exception as _ke:
        kayak = None
        kayak_hub = None
        character_gateway.set_bridge(None)
        KAYAK_ENABLED = False
        logging.warning(f'KAYAK: Import/connect failed ({_ke}) - falling back to native prompts.')

def _stop_kayak_autostarted():
    """Stop Kayak only if this server spawned it."""
    global _KAYAK_PROCESS, _KAYAK_STARTED_BY_SERVER
    if not _KAYAK_STARTED_BY_SERVER:
        return
    try:
        if _KAYAK_PROCESS is not None and _KAYAK_PROCESS.poll() is None:
            _pid = _KAYAK_PROCESS.pid
            logging.info(f"SYSTEM: Stopping Kayak auto-started process (PID {_pid})...")
            try:
                subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(_pid)],
                    capture_output=True,
                    shell=False,
                    timeout=5
                )
            except Exception:
                try:
                    _KAYAK_PROCESS.terminate()
                except Exception:
                    pass
    except Exception as _kstop_e:
        logging.warning(f"SYSTEM: Failed to stop Kayak child process cleanly ({_kstop_e})")
    finally:
        _KAYAK_PROCESS = None
        _KAYAK_STARTED_BY_SERVER = False

def _shutdown_server(reason: str):
    """Best-effort cleanup before hard process exit."""
    try:
        logging.info(reason)
    except Exception:
        pass
    try:
        _stop_kayak_autostarted()
    finally:
        os._exit(0)

_kayak_try_connect()  # added by Pineaxe v04 - initial connect attempt at startup
atexit.register(_stop_kayak_autostarted)

def _signal_shutdown_handler(signum, _frame):
    _shutdown_server(f"SYSTEM: Received signal {signum}. Shutting down server.")

for _sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
    if _sig is None:
        continue
    try:
        signal.signal(_sig, _signal_shutdown_handler)
    except Exception:
        pass

# Kill any existing process on port 5000 before starting
def _kayak_health_monitor():
    """Background thread to periodically check and restart Kayak if unreachable."""
    logging.info("KAYAK: Health monitor thread started.")
    while True:
        try:
            # Only try to connect/restart every 30s
            time.sleep(30)
            
            global KAYAK_ENABLED, kayak_hub
            
            # If Kayak was never started OR current hub is unreachable/broken
            needs_restart = not KAYAK_ENABLED
            if kayak_hub and hasattr(kayak_hub, "_circuit_broken"):
                if kayak_hub._circuit_broken:
                    needs_restart = True
            
            if needs_restart:
                logging.debug("KAYAK: Health monitor attempting reconnect...")
                _kayak_try_connect()
                
        except Exception as e:
            logging.error(f"KAYAK: Error in health monitor: {e}")

# Start health monitor thread
threading.Thread(target=_kayak_health_monitor, daemon=True).start()

# Kill any existing process on port 5000 before starting
def _is_local_port_open(port: int) -> bool:
    for _host in ("127.0.0.1", "localhost"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.settimeout(0.2)
                if _s.connect_ex((_host, port)) == 0:
                    return True
        except Exception:
            continue
    return False


def _get_monitored_parent_pid() -> int:
    """Return the process PID that auto-shutdown should monitor."""
    raw = os.environ.get("SENTIENT_SANDS_MONITORED_PARENT_PID", "").strip()
    if raw:
        try:
            pid = int(raw)
            if pid > 1:
                return pid
        except Exception:
            logging.warning(f"SYSTEM: Invalid SENTIENT_SANDS_MONITORED_PARENT_PID={raw!r}; falling back to os.getppid().")
    return os.getppid()

def _spawn_replacement_server() -> bool:
    """Start a replacement server process using the same interpreter/script."""
    try:
        _script = os.path.abspath(__file__)
        _python = sys.executable
        _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _env = os.environ.copy()
        # Preserve the original Kenshi parent PID across self-restarts. Without
        # this, the replacement process may monitor the old Python server and
        # immediately shut itself down when the old process exits.
        _env["SENTIENT_SANDS_MONITORED_PARENT_PID"] = str(_get_monitored_parent_pid())
        subprocess.Popen(
            [_python, _script],
            cwd=SCRIPT_DIR,
            creationflags=_flags,
            env=_env,
        )
        return True
    except Exception as _spawn_e:
        logging.error(f"SYSTEM: Failed to spawn replacement server: {_spawn_e}")
        return False

def _restart_self_async(reason: str):
    """Schedule self-restart so HTTP caller receives a response first."""
    def _worker():
        time.sleep(0.45)
        # Persist active pointers so replacement boots into same context.
        try:
            save_settings({"current_model": CURRENT_MODEL_KEY, "current_campaign": ACTIVE_CAMPAIGN})
        except Exception:
            pass
        if _spawn_replacement_server():
            _shutdown_server(reason)
        else:
            logging.error("SYSTEM: Restart aborted — replacement process was not created.")
    threading.Thread(target=_worker, daemon=True).start()

# Flask app initialization
app = Flask(__name__, template_folder=os.path.join(KENSHI_SERVER_DIR, "templates"))
# JSON_AS_ASCII = True is default, which is safer for our DLL pipe
app.config['JSON_AS_ASCII'] = True


@app.errorhandler(Exception)
def handle_exception(e):
    # Determine status code
    code = 500
    if hasattr(e, 'code'):
        code = e.code

    # Suppress noisy stack traces for 404s
    if code == 404:
        logging.info(f"ROUTE: 404 Not Found - {request.method} {request.path}")
        return jsonify({"error": "Resource not found", "status": "error"}), 404

    # Log the full stack trace for any other unhandled exception in Flask routes
    logging.error(f"UNHANDLED SERVER EXCEPTION: {str(e)}")
    debug_logger.error(f"UNHANDLED SERVER EXCEPTION STACK:\n{traceback.format_exc()}")
    # Truncate request data if possible for the debug log
    try:
        if request.json:
            debug_logger.debug(f"Offending Request JSON: {json.dumps(request.json, indent=2)}")
    except:
        pass
    return jsonify({"error": str(e), "status": "error"}), 500

# 3. Load Configurations
def load_configs():
    global MODELS_CONFIG, PROVIDERS_CONFIG, NAMES_CONFIG
    logging.debug("Checking configurations...")
    
    # Create config dir if missing
    config_dir = os.path.join(KENSHI_SERVER_DIR, "config")
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)

    if os.path.exists(MODELS_PATH):
        try:
            with open(MODELS_PATH, "r") as f:
                MODELS_CONFIG = json.load(f)
            logging.debug(f"Loaded {len(MODELS_CONFIG)} models.")
        except Exception as e:
            logging.error(f"Failed to load models.json: {e}")
            
    if os.path.exists(PROVIDERS_PATH):
        try:
            with open(PROVIDERS_PATH, "r") as f:
                PROVIDERS_CONFIG = json.load(f)
            logging.debug(f"Loaded {len(PROVIDERS_CONFIG)} providers.")
        except Exception as e:
            logging.error(f"Failed to load providers.json: {e}")

    set_model_configs(MODELS_CONFIG, PROVIDERS_CONFIG)

    load_item_aliases(os.path.join(KENSHI_SERVER_DIR, "config", "item_aliases.json"))

    if os.path.exists(NAMES_PATH):
        try:
            with open(NAMES_PATH, "r") as f:
                NAMES_CONFIG = json.load(f)
            logging.debug(f"Loaded {len(NAMES_CONFIG)} gender pools from names.json.")
        except Exception as e:
            logging.error(f"Failed to load names.json: {e}")

    if os.path.exists(GENERIC_NAMES_PATH):
        try:
            global GENERIC_CONFIG
            with open(GENERIC_NAMES_PATH, "r") as f:
                GENERIC_CONFIG = json.load(f)
            logging.debug(f"Loaded {len(GENERIC_CONFIG.get('prefixes', []))} generic prefixes from generic_names.json.")
        except Exception as e:
            logging.error(f"Failed to load generic_names.json: {e}")

    global LOCALIZATION_CONFIG
    LOCALIZATION_CONFIG = {}
    if os.path.exists(LOCALIZATION_PATH):
        try:
            with open(LOCALIZATION_PATH, "r", encoding="utf-8") as f:
                LOCALIZATION_CONFIG = json.load(f)
            logging.debug(f"Loaded {len(LOCALIZATION_CONFIG)} language localizations.")
        except Exception as e:
            logging.error(f"Failed to load localization.json: {e}")

    load_renaming_config()


def load_renaming_config():
    """Load renaming_list.txt and renaming_rules.txt from config/.

    renaming_list.txt: one exact title-only name per line.
    renaming_rules.txt: key = value pairs controlling rename behavior.
    """
    global RENAMING_LIST, RENAMING_RULES

    # Load renaming list (exact match titles)
    RENAMING_LIST = set()
    if os.path.exists(RENAMING_LIST_PATH):
        try:
            with open(RENAMING_LIST_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        RENAMING_LIST.add(stripped)
            logging.info(f"Loaded {len(RENAMING_LIST)} entries from renaming_list.txt")
        except Exception as e:
            logging.error(f"Failed to load renaming_list.txt: {e}")
    else:
        logging.warning(f"renaming_list.txt not found at {RENAMING_LIST_PATH} — using legacy heuristic")

    # Load renaming rules
    RENAMING_RULES = {
        "mode": "title_only_expand",
        "preserve_existing_named": True,
        "match": "exact",
    }
    if os.path.exists(RENAMING_RULES_PATH):
        try:
            with open(RENAMING_RULES_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" in stripped:
                        key, val = stripped.split("=", 1)
                        key = key.strip().lower()
                        val = val.strip()
                        if val.lower() in ("true", "yes", "1"):
                            RENAMING_RULES[key] = True
                        elif val.lower() in ("false", "no", "0"):
                            RENAMING_RULES[key] = False
                        else:
                            RENAMING_RULES[key] = val
            logging.info(f"Loaded renaming_rules.txt: {RENAMING_RULES}")
        except Exception as e:
            logging.error(f"Failed to load renaming_rules.txt: {e}")

# Event History Persistence
GLOBAL_EVENT_COUNTER = 0

# --- CAMPAIGN MANAGEMENT ---
def get_campaign_dir():
    if not os.path.exists(CAMPAIGNS_DIR):
        os.makedirs(CAMPAIGNS_DIR)
        logging.info(f"Created base campaigns directory: {CAMPAIGNS_DIR}")
        
    cdir = os.path.join(CAMPAIGNS_DIR, ACTIVE_CAMPAIGN)
    if not os.path.exists(cdir):
        os.makedirs(cdir)
        logging.info(f"Created campaign directory: {cdir}")
        # Automatically seed new campaigns created during startup/init
        ensure_campaign_seeded(cdir)
    return cdir


def ensure_campaign_seeded(cdir):
    """Populates a campaign directory with default templates and folders."""
    try:
        if not os.path.exists(os.path.join(cdir, "characters")):
            os.makedirs(os.path.join(cdir, "characters"))
            
        # Copy essential personal files to campaigns by default. 
        # All other templates (rules, lore, etc.) remain global in TEMPLATES_DIR.
        for component in ["character_bio.txt", "player_faction_description.txt"]:
            src = os.path.join(TEMPLATES_DIR, component)
            dst = os.path.join(cdir, component)
            if os.path.exists(src) and not os.path.exists(dst):
                import shutil
                shutil.copy2(src, dst)
                logging.info(f"CAMPAIGN: Seeded '{os.path.basename(cdir)}' with {component}")
            
        # Ensure world_events.txt exists (Campaign-Specific History)
        ev_path = os.path.join(cdir, "world_events.txt")
        if not os.path.exists(ev_path):
            with open(ev_path, "w", encoding="utf-8") as f:
                f.write("# Dynamic rumors generated for this campaign\n")

        # Ensure campaign_chronicle.json exists (Persistent Major Events)
        chronicle_path = os.path.join(cdir, "campaign_chronicle.json")
        if not os.path.exists(chronicle_path):
            with open(chronicle_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            logging.info(f"CAMPAIGN: Seeded '{os.path.basename(cdir)}' with campaign_chronicle.json")
    except Exception as e:
        logging.error(f"Failed to seed campaign directory {cdir}: {e}")

def migrate_to_campaigns():
    """Moves legacy data to campaigns/Default if not already migrated."""
    try:
        if not os.path.exists(CAMPAIGNS_DIR):
            os.makedirs(CAMPAIGNS_DIR)
            
        default_dir = os.path.join(CAMPAIGNS_DIR, "Default")
        is_new_default = not os.path.exists(default_dir)
        
        if is_new_default:
            os.makedirs(default_dir)
            logging.info("MIGRATION: Created Default campaign folder")
            
        import shutil
        # 1. Characters
        old_chars = os.path.join(KENSHI_SERVER_DIR, "characters")
        new_chars = os.path.join(default_dir, "characters")
        if os.path.exists(old_chars) and not os.path.exists(new_chars):
            try:
                shutil.move(old_chars, new_chars)
                logging.info("MIGRATION: Moved legacy characters to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (Characters): {e}")
            
        # 2. Registry
        old_reg = os.path.join(KENSHI_MOD_DIR, "kenshi_ai_registry")
        if not os.path.exists(old_reg):
            old_reg = os.path.join(KENSHI_MOD_DIR, "sentient_sands_registry")
        
        new_reg = os.path.join(default_dir, "sentient_sands_registry")
        if os.path.exists(old_reg) and not os.path.exists(new_reg):
            try:
                shutil.move(old_reg, new_reg)
                logging.info("MIGRATION: Moved legacy registry to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (Registry): {e}")

        # 3. World Events / Rumors
        old_events = os.path.join(KENSHI_SERVER_DIR, "world_events.txt")
        new_events = os.path.join(default_dir, "world_events.txt")
        if os.path.exists(old_events) and not os.path.exists(new_events):
            try:
                shutil.move(old_events, new_events)
                logging.info("MIGRATION: Moved legacy world_events.txt to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (World Events): {e}")

        # 4. Global Event History
        old_hist = os.path.join(KENSHI_SERVER_DIR, "event_history.json")
        new_hist = os.path.join(default_dir, "event_history.json")
        if os.path.exists(old_hist) and not os.path.exists(new_hist):
            try:
                shutil.move(old_hist, new_hist)
                logging.info("MIGRATION: Moved legacy event_history.json to campaigns/Default")
            except Exception as e:
                logging.error(f"MIGRATION ERROR (History): {e}")

        # Ensure templates exist in Default (always check this during migration)
        ensure_campaign_seeded(default_dir)
            
    except Exception as e:
        logging.error(f"MIGRATION: Critical failure in migration logic: {e}")

def load_campaign_config():
    """Initializes paths based on the active campaign."""
    global CHARACTERS_DIR, EVENT_HISTORY, EVENT_HISTORY_SET
    try:
        cdir = get_campaign_dir()
        
        # 1. Update Directories
        CHARACTERS_DIR = os.path.join(cdir, "characters")
        if not os.path.exists(CHARACTERS_DIR): 
            os.makedirs(CHARACTERS_DIR)
        
        # 2. Load Persisted Event History
        hist_path = os.path.join(cdir, "event_history.json")
        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    EVENT_HISTORY = json.load(f)
                EVENT_HISTORY_SET = set(EVENT_HISTORY)
                logging.info(f"CAMPAIGN: Loaded {len(EVENT_HISTORY)} events for '{ACTIVE_CAMPAIGN}'")
            except Exception as e:
                logging.error(f"Failed to load event history: {e}")
                EVENT_HISTORY = []
                EVENT_HISTORY_SET = set()
        else:
            EVENT_HISTORY = []
            EVENT_HISTORY_SET = set()

        # 3. Load Shop Stock cache
        refresh_shop_stock_cache(force=True)

        # 4. Push generic names to DLL
        push_generic_names_to_dll()
    except Exception as e:
        logging.error(f"CAMPAIGN: Critical failure loading config: {e}")


def refresh_shop_stock_cache(force=False):
    """Refresh shop_stock.json when it changes on disk."""
    global SHOP_STOCK, SHOP_STOCK_MTIME

    try:
        shop_stock_path = os.path.join(get_campaign_dir(), "shop_stock.json")
    except Exception:
        return SHOP_STOCK

    if not os.path.exists(shop_stock_path):
        if force or SHOP_STOCK:
            SHOP_STOCK = {}
            SHOP_STOCK_MTIME = 0.0
        return SHOP_STOCK

    try:
        mtime = os.path.getmtime(shop_stock_path)
    except OSError:
        return SHOP_STOCK

    if not force and mtime == SHOP_STOCK_MTIME:
        return SHOP_STOCK

    try:
        with open(shop_stock_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cleaned = {}
        for key, value in raw.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, list):
                items = [str(item).strip() for item in value if str(item).strip()]
            else:
                items = []
            cleaned[str(key)] = items
        SHOP_STOCK = cleaned
        SHOP_STOCK_MTIME = mtime
        logging.info(f"CAMPAIGN: Loaded shop stock for {len(SHOP_STOCK)} NPCs")
    except Exception as e:
        logging.error(f"Failed to load shop_stock.json: {e}")
        SHOP_STOCK = {}
        SHOP_STOCK_MTIME = 0.0

    return SHOP_STOCK


def _normalize_shop_stock_key(name):
    text = _clean_npc_name(name)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return re.sub(r"[^a-z0-9]", "", text)


def get_shop_stock_for_npc(npc_name):
    """Return cached shop stock for one NPC, matching common name variants."""
    refresh_shop_stock_cache()
    if not SHOP_STOCK or not npc_name:
        return None

    raw_name = str(npc_name or "").strip()
    clean_name = _clean_npc_name(raw_name)
    variants = []
    for candidate in (
        raw_name,
        clean_name,
        _safe_char_filename(clean_name),
        re.sub(r"[^\w\-]", "_", clean_name).strip("_"),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)

    for candidate in variants:
        if candidate in SHOP_STOCK:
            return list(SHOP_STOCK.get(candidate) or [])

    wanted = _normalize_shop_stock_key(clean_name)
    if not wanted:
        return None

    for key, items in SHOP_STOCK.items():
        if _normalize_shop_stock_key(key) == wanted:
            return list(items or [])

    return None


def _shop_stock_matches_item(stock_items, requested_item):
    """True when an item request matches a cached shop-stock entry."""
    if not stock_items:
        return False

    req_base, _ = _split_item_and_count(requested_item)
    req_norm = normalize_trade_item_name(req_base).strip().lower()
    if not req_norm:
        return False

    for stock_name in stock_items:
        stock_base, _ = _split_item_and_count(stock_name)
        stock_norm = normalize_trade_item_name(stock_base).strip().lower()
        if not stock_norm:
            continue
        if req_norm == stock_norm or req_norm in stock_norm or stock_norm in req_norm:
            return True
    return False

def send_to_pipe(cmd):
    """
    Robust pipe transmission. Prepends CMD: if not already present.
    """
    if not (cmd.startswith("CMD:") or cmd.startswith("NPC_") or cmd.startswith("PLAYER_") or cmd.startswith("SHOW_HISTORY") or cmd.startswith("NOTIFY:")):
        cmd = "CMD: " + cmd
        
    try:
        with open(r'\\.\pipe\SentientSands', 'wb') as f:
            f.write(cmd.encode('utf-8'))
    except:
        pass

def push_generic_names_to_dll():
    """Syncs the renaming title list to the C++ DLL via pipe.

    The DLL uses this list to decide which NPCs to send to
    /get_batch_identities for renaming. We send the exact-match
    titles from renaming_list.txt (RENAMING_LIST), plus the legacy
    prefixes/keywords from generic_names.json as a fallback for
    older DLL builds that expect the old format.
    """
    try:
        # New: send exact titles from renaming_list.txt
        if RENAMING_LIST:
            titles_str = ",".join(sorted(RENAMING_LIST))
            send_to_pipe(f"POPULATE_RENAME_TITLES: {titles_str}")
            logging.info(f"PIPE: Synced {len(RENAMING_LIST)} rename titles to DLL")

        # Legacy: also send old-format prefixes/keywords for backwards compatibility
        prefixes = GENERIC_CONFIG.get("prefixes", [])
        keywords = GENERIC_CONFIG.get("keywords", [])
        if prefixes or keywords:
            p_str = ",".join(prefixes)
            k_str = ",".join(keywords)
            send_to_pipe(f"POPULATE_GENERIC: {p_str}|{k_str}")
    except Exception as e:
        logging.error(f"Failed to sync generic names to DLL: {e}")

def save_campaign_history():
    try:
        cdir = get_campaign_dir()
        hist_path = os.path.join(cdir, "event_history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(EVENT_HISTORY, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to save event history: {e}")



def is_npc_name_generic(name):
    """Check if an NPC name is a title-only generic eligible for renaming.

    Uses EXACT MATCH against renaming_list.txt (loaded into RENAMING_LIST).
    No substring matching. No regex guessing.

    "Paladin"              → True  (exact match in list)
    "Paladin Salek"        → False (not in list — already has a name)
    "High Inquisitor Valtena" → False (not in list)

    Falls back to legacy heuristic only if renaming_rules.txt sets match=legacy.
    """
    if not name:
        return True

    clean_name = str(name).split('|')[0].strip()
    if not clean_name:
        return True

    match_mode = RENAMING_RULES.get("match", "exact")

    if match_mode == "exact":
        # Primary path: exact match against the externally-defined title list.
        return clean_name in RENAMING_LIST

    # Legacy fallback (match=legacy in renaming_rules.txt)
    return _legacy_is_generic(clean_name)


def _legacy_is_generic(clean_name):
    """Old heuristic detection. Only used when renaming_rules.txt sets match=legacy."""
    if clean_name in GENERIC_NAMES:
        return True
    prefixes = GENERIC_CONFIG.get("prefixes", [])
    keywords = GENERIC_CONFIG.get("keywords", [])
    lower_clean = clean_name.lower()
    if any(p.lower() in lower_clean for p in prefixes):
        return True
    if any(k.lower() in lower_clean for k in keywords):
        return True
    return False


# Legacy hardcoded list — only used when match=legacy in renaming_rules.txt.
# The primary system uses renaming_list.txt (loaded into RENAMING_LIST).
GENERIC_NAMES = [
    "Hungry Bandit", "Dust Bandit", "Starving Vagrant", "Drifter", "Samurai", 
    "Holy Sentinel", "Holy Servant", "Swamper", "Tech Hunter", "Mercenary",
    "Shop Guard", "Caravan Guard", "Slave Hunter", "Slaver", "Manhunter",
    "Escaped Slave", "Rebirth Slave", "United Cities Citizen", "Holy Nation Citizen",
    "Shek Warrior", "Hive Worker", "Hive Soldier", "Hive Prince", "Fogman",
    "Barman", "Pacifier", "Bar Thug",
    "Cannibal", "Outlaw", "Farmer", "Nomad", "Trader", "Gate Guard", 
    "Unknown Entity", "Someone", "Mercenary Heavy", "Mercenary Captain",
    "Holy Nation Outlaw", "Holy Nation Peasant", "United Cities Peasant",
    "Wandering Assassin", "Trader Guard", "Hiver Ronin", "Skeleton Legion",
    "Reaver", "Grass Pirate", "Black Dog", "Crab Raider", "Skeleton Bandit"
]

KENSHI_NAME_POOL = [
    "Kaelen", "Korg", "Vayn", "Sark", "Mina", "Rook", "Drake", "Silas", "Tane", "Kuna",
    "Zarek", "Jorn", "Lyra", "Kael", "Brena", "Torin", "Sola", "Fen", "Krax", "Vora",
    "Dax", "Nyx", "Garek", "Sora", "Thane", "Kira", "Zane", "Lara", "Marek", "Vina",
    "Rel", "Kaan", "Siv", "Tork", "Meda", "Grox", "Vael", "Syra", "Keld", "Bara",
    "Dorn", "Neld", "Gora", "Skarn", "Vane", "Kura", "Zora", "Lena", "Morn", "Vela",
    "Rael", "Kona", "Sima", "Teld", "Mora", "Grak", "Veld", "Sura", "Karn", "Bena",
    "Drak", "Nala", "Gord", "Sina", "Vara", "Kela", "Zana", "Lina", "Mela", "Vorna",
    "Hark", "Skal", "Vorn", "Grek", "Myla", "Rion", "Daka", "Sith", "Tyla", "Korr",
    "Zent", "Lyr", "Brax", "Vort", "Nara", "Grel", "Syk", "Tarn", "Moko", "Vull",
    "Kess", "Tory", "Vann", "Sael", "Miro", "Lorn", "Gryf", "Dael", "Seld", "Kurv"
]

# get_used_names lives in ss_persistence — imported above

def generate_unique_lore_name(gender="Neutral", used_names=None, title_prefix=""):
    """Generate a unique name, optionally prepending the NPC's original title.

    In title_only_expand mode:  "Paladin" + "Tevar" → "Paladin Tevar"
    In full_replace mode:       "Paladin" → "Tevar" (title discarded)
    """
    used = used_names if used_names is not None else get_used_names()
    
    gender_key = "Neutral"
    if gender.lower() == "male": gender_key = "Male"
    elif gender.lower() == "female": gender_key = "Female"
    
    pool = NAMES_CONFIG.get(gender_key, [])
    if not pool and gender_key != "Neutral":
        pool = NAMES_CONFIG.get("Neutral", [])
    if not pool:
        pool = KENSHI_NAME_POOL
    
    mode = RENAMING_RULES.get("mode", "title_only_expand")

    def _make_full(base_name):
        if mode == "title_only_expand" and title_prefix:
            return f"{title_prefix} {base_name}"
        return base_name

    # Try to find an unused name
    available = [n for n in pool if _make_full(n).lower() not in used]
    if available:
        return _make_full(random.choice(available))

    # Fallback: numbered suffix
    base = random.choice(pool if pool else KENSHI_NAME_POOL)
    for i in range(1, 1000):
        candidate = _make_full(f"{base} {i}")
        if candidate.lower() not in used:
            return candidate
    return _make_full(f"{base}_{random.randint(1000, 9999)}")

# Имя говорящего короткое и без знаков конца предложения. Ответ NPC вроде
# «У меня стройка: доски, ткани» двоеточие содержит, но префиксом не является:
# раньше такая реплика уходила в историю без имени, а в глобальные события —
# с целым абзацем вместо имени актёра.
_SPEAKER_PREFIX_MAX_CHARS = 48


def _has_speaker_prefix(line):
    """True, если строка начинается с «Имя: », а не просто содержит двоеточие."""
    head, sep, _ = str(line or "").partition(":")
    if not sep:
        return False
    head = head.strip()
    if not head or len(head) > _SPEAKER_PREFIX_MAX_CHARS:
        return False
    return not any(ch in head for ch in ".!?,;")


# Отпечаток сделки: что выдано и сколько взято. Нужен, чтобы отличить
# повторную оплату того же заказа от нового.
_TRADE_TAG_RE = re.compile(
    r"\[ACTION:\s*(GIVE_ITEM|SPAWN_ITEM|TAKE_CATS)\s*:([^\]]*)\]", re.IGNORECASE)
_HISTORY_PREFIX_RE = re.compile(r"^\[Day[^\]]*\]\s*(?:\(Overheard\)\s*)?")


def _trade_signature(text):
    """Сравнимый набор торговых тегов строки или списка действий."""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text)
    found = []
    for kind, body in _TRADE_TAG_RE.findall(str(text or "")):
        found.append(f"{kind.upper()}:{' '.join(body.split()).casefold()}")
    return tuple(sorted(found))


def _last_own_history_line(history, npc_name):
    """Последняя реплика самого NPC в его истории, без штампа времени."""
    target = str(_clean_npc_name(npc_name) or "").casefold()
    if not target:
        return ""
    for raw in reversed(list(history or [])):
        line = _HISTORY_PREFIX_RE.sub("", str(raw or ""))
        if not _has_speaker_prefix(line):
            continue
        head = line.partition(":")[0].split("|")[0].strip()
        if str(_clean_npc_name(head) or "").casefold() == target:
            return line
    return ""


# Служебные теги, которые срезаются перед отправкой реплики в игру. Если
# после них не остаётся ни слова, игрок видит «...» и немого собеседника.
_SERVICE_TAG_RE = re.compile(
    r"\[\s*(?:ACTION|TASK|TAG|STATUS|EFFECT|EMOTE|THOUGHT|JUDGMENT)"
    r"(?:\s*:\s*[^\]]+)?\s*\]", re.IGNORECASE)
# Теги, меняющие мир. В отличие от JUDGMENT их нельзя потерять при переспросе.
_GAME_TAG_RE = re.compile(r"\[\s*(?:ACTION|TASK)\s*:[^\]]*\]", re.IGNORECASE)


def _spoken_text(reply):
    """Что останется от ответа модели, когда служебные теги срежут."""
    return _SERVICE_TAG_RE.sub("", str(reply or "")).strip()


def _service_notice(language, ru, en):
    """Служебное сообщение игроку на языке игры.

    Это не реплика персонажа, а объяснение, почему её нет: пузырь речи —
    единственный канал до игрока, который у мода есть.
    """
    lang = str(language or "").strip().lower()
    return ru if lang.startswith(("rus", "рус")) else en


def get_current_time_prefix():
    if PLAYER_CONTEXT:
        day = PLAYER_CONTEXT.get('day', 0)
        hour = int(PLAYER_CONTEXT.get('hour', 0))
        minute = int(PLAYER_CONTEXT.get('minute', 0))
        return f"[Day {day}, {hour:02d}:{minute:02d}] "
    return ""


def _persist_conversation_history_target(name, char_datas, relation_dirty_names=None, persist_source="chat_persist"):
    """Persist one loaded profile's dialogue history using the chat-safe write path."""
    if not name or name not in char_datas or not char_datas[name]:
        return

    relation_dirty_names = set(relation_dirty_names or ())
    profile = char_datas[name]
    if "ConversationHistory" not in profile:
        profile["ConversationHistory"] = []

    if len(profile["ConversationHistory"]) > DIALOGUE_HISTORY_LIMIT:
        profile["ConversationHistory"] = profile["ConversationHistory"][-DIALOGUE_HISTORY_LIMIT:]

    storage_id = profile.get("ID", name)
    has_history = len(profile.get("ConversationHistory", [])) > 0
    profile["_has_dialogue"] = "1" if has_history else str(profile.get("_has_dialogue") or "0")
    is_pending_profile = profile_needs_upgrade(profile)
    hydration_job = _load_hydration_job(name=name, storage_id=storage_id, campaign=ACTIVE_CAMPAIGN)
    is_hydrating_profile = _profile_is_hydrating(profile) or bool(hydration_job)
    _persist_persona = get_persona_category(
        profile.get("Race", "Unknown"),
        profile.get("Faction", "Unknown"),
        name=name,
        source=persist_source,
    )
    profile["_persona_category"] = _persist_persona

    if is_hydrating_profile:
        _, _hydration_live_ctx = resolve_live_context(
            name=name,
            context=profile,
            explicit_id=profile.get("ID") or storage_id,
        )
        _hydration_live_ctx = _hydration_live_ctx or {}
        _synced_job = _sync_hydration_job(
            profile,
            name=name,
            storage_id=storage_id,
            campaign=ACTIVE_CAMPAIGN,
            live_ctx=_hydration_live_ctx,
        )
        hydrated_profile, hydrate_ok = _run_hydration_job(
            _synced_job,
            campaign=ACTIVE_CAMPAIGN,
            live_ctx=_hydration_live_ctx,
        ) if _synced_job else (None, False)
        if hydrate_ok and hydrated_profile:
            char_datas[name] = hydrated_profile
            _pending_discard(
                name=name,
                context=char_datas[name],
                explicit_id=char_datas[name].get("ID") or storage_id,
                live_ctx=_hydration_live_ctx,
                storage_id=storage_id,
                profile=char_datas[name],
            )
            logging.info(f"PERSIST: hydrated first-contact profile for {name}")
        else:
            logging.info(f"PERSIST: staged hydrating profile for {name}; durable job retained")
        return

    if is_pending_profile:
        _, _pending_live_ctx = resolve_live_context(
            name=name,
            context=profile,
            explicit_id=profile.get("ID") or storage_id,
        )
        _stored_pending = _pending_store(
            profile,
            name=name,
            context=profile,
            explicit_id=(profile.get("ID") or storage_id),
            live_ctx=_pending_live_ctx,
            storage_id=storage_id,
        )
        char_datas[name] = _stored_pending or profile
        logging.info(f"PERSIST: updated server-side pending NPC memory for {name}")
        return

    if _persist_persona == "feral":
        logging.debug(f"SKIP SAVE: {name} is feral; blocking entity persistence.")
        return

    if profile.get("_transient") and not has_history:
        logging.debug(f"SKIP SAVE: {name} is using a transient fallback profile with no history. Blocking disk override.")
        return

    if not should_save_profile(name, storage_id, profile):
        return

    if name in relation_dirty_names:
        character_gateway.write(
            profile,
            campaign=ACTIVE_CAMPAIGN,
            reason="relation_persist",
        )
        return

    _dlg_ok = character_gateway.write_dialogue_only(
        name,
        storage_id,
        profile["ConversationHistory"],
        campaign=ACTIVE_CAMPAIGN,
    )
    if not _dlg_ok:
        logging.info(f"PERSIST: write_dialogue_only failed for {name}, falling back to full write()")
        character_gateway.write(
            profile,
            campaign=ACTIVE_CAMPAIGN,
            reason="chat_history_persist",
        )


def _is_player_side_faction(faction_name: str) -> bool:
    """
    True when a faction string represents the player's side.
    Used by yell action filtering (Python-side only, no DLL changes).
    """
    faction = str(faction_name or "").strip()
    if not faction:
        return False

    f = faction.lower()
    player_faction = str(get_effective_player_context().get("faction", "") or "").strip().lower()

    if player_faction and f == player_faction:
        return True

    # Common display forms used by logs/context.
    if "player's squad" in f or "players squad" in f or f.startswith("player squad"):
        return True

    # Legacy/neutral player-side marker.
    if f == "nameless":
        return True

    return False

def generate_relation_bar(rel):
    """Generates a text-based visual representation of the NPC's relation to the player."""
    try:
        rel = int(rel)
    except:
        rel = 0
    
    # Scale: -100 to 100
    # Normalize -100..100 to 0..20 dashes
    pos = int((rel + 100) / 10)
    pos = max(0, min(20, pos))
    
    bar = list("---------------------")
    bar[pos] = "X" # Marker
    bar_str = "".join(bar)
    
    # Status Label
    label = "NEUTRAL"
    if rel <= -90: label = "ARCH-ENEMY"
    elif rel <= -60: label = "HOSTILE"
    elif rel <= -25: label = "UNFRIENDLY"
    elif rel >= 90: label = "SOUL-MATE"
    elif rel >= 60: label = "ALLIED"
    elif rel >= 25: label = "FRIENDLY"
    
    # Add color tags for MyGUI (if supported, using # prefix)
    # Actually, let's keep it plain text for max compatibility across UI versions
    return f"RELATION: [{label}] [{bar_str}] ({rel:+} pts)"

def is_future_timestamp(line, cur_d, cur_h, cur_m):
    """Checks if a string containing [Day X, HH:MM] is ahead of the provided current time."""
    match = re.search(r"\[Day (\d+)(?:, (\d+):(\d+))?\]", line)
    if not match: return False
    d = int(match.group(1))
    h = int(match.group(2)) if match.group(2) else 0
    m = int(match.group(3)) if match.group(3) else 0
    if d > cur_d: return True
    if d < cur_d: return False
    if h > cur_h: return True
    if h < cur_h: return False
    return m > cur_m


def _split_item_and_count(arg_text: str):
    """
    Split item args into (base_name, count_suffix) where count_suffix is ':N'
    parsed from the last top-level colon (ignores colons inside brackets).
    """
    text = str(arg_text or "").strip()
    if not text:
        return "", ""
    depth = 0
    split_at = -1
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == ']':
            depth += 1
        elif ch == '[':
            depth -= 1
        elif ch == ':' and depth == 0:
            split_at = i
            break
    if split_at == -1:
        return text, ""
    rhs = text[split_at + 1:].strip()
    if rhs.isdigit():
        return text[:split_at].strip(), f":{rhs}"
    return text, ""


def _normalize_item_arg_preserve_count(arg_text: str) -> str:
    base, cnt = _split_item_and_count(arg_text)
    normalized = normalize_trade_item_name(base.strip())
    return f"{normalized}{cnt}" if cnt else normalized


def _normalize_spawn_args(arg_text: str) -> str:
    text = str(arg_text or "").strip()
    if not text:
        return text
    if "|" in text:
        templ, rest = text.split("|", 1)
        templ_norm = _normalize_item_arg_preserve_count(templ.strip())
        return templ_norm + " | " + rest.lstrip()
    return _normalize_item_arg_preserve_count(text)


def _normalize_direct_action_tag(tag: str) -> str:
    """
    Normalize item aliases in direct action tags (e.g. /k_speak).
    Supports GIVE/TAKE/DROP/SPAWN item tags and preserves :count suffix.
    """
    raw = str(tag or "").strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return raw
    inner = raw[1:-1].strip()
    if ":" not in inner:
        return raw
    kind, rest = inner.split(":", 1)
    if kind.strip().upper() not in ("ACTION", "TASK"):
        return raw
    rest = rest.strip()
    if ":" in rest:
        kw, args = rest.split(":", 1)
        kw = kw.strip().upper()
        args = args.strip()
    else:
        kw, args = rest.strip().upper(), ""

    if not args:
        return raw

    new_args = args
    if kw in ("GIVE_ITEM", "TAKE_ITEM", "DROP_ITEM"):
        new_args = _normalize_item_arg_preserve_count(args)
    elif kw == "SPAWN_ITEM":
        new_args = _normalize_spawn_args(args)

    if new_args == args:
        return raw
    return f"[ACTION: {kw}: {new_args}]"


# Mappings for Kenshi enums
SHORT_TERM_MEM = {
    1: "INTRUDER", 2: "AGGRESSOR", 3: "TEMPORARY_ALLY", 4: "TEMPORARY_ENEMY",
    5: "PRISONER", 6: "HAS_BEEN_LOOTED", 7: "CRIMINAL"
}
LONG_TERM_MEM = {
    1: "MY_INTRUDER", 2: "MY_LIFESAVER", 3: "FREED_ME", 4: "STOLE_FROM_ME",
    5: "MY_CAPTOR", 6: "FRIENDLY_AQUAINTANCE", 7: "DEFEATED_MY_SQUAD_ONCE",
    8: "SQUAD_LOST_TO_ME_ONCE", 14: "KILLED_MY_FRIEND", 15: "I_SCREWED_THIS_GUY"
}

# _clean_npc_name and _is_strong_uid live in ss_persistence — imported above
# _build_name_faction_id lives in ss_identity — imported above

# _is_strong_uid lives in ss_persistence — imported above

def _preferred_storage_id(name="", faction=None, *candidates, uid=None):
    clean_name = _clean_npc_name(name)
    derived = _build_name_faction_id(clean_name, faction) if clean_name else ""

    if _is_strong_uid(uid):
        return str(uid).strip()

    for candidate in candidates:
        if candidate is None:
            continue
        sid = str(candidate).strip()
        if not sid:
            continue
        # Current DLL builds often send storage_id=name. If we also know faction,
        # prefer the stronger derived name+faction ID instead of the weak alias.
        if clean_name and sid == clean_name and derived and derived != sid:
            continue
        return sid

    return derived


# Identity resolution functions live in ss_identity — imported above
# (_normalize_context_identity through _DirectChatLease)


# Prompt assembly functions live in ss_prompt — imported above
# (build_detailed_context_string, load_prompt_component, format_player_status,
#  format_player_inventory, fetch_dynamic_lore, build_events_block, build_system_prompt)

def update_world_index():
    global WORLD_INDEX
    try:
        WORLD_INDEX = build_world_index()
        logging.info(f"World Index Updated: {len(WORLD_INDEX)} names indexed from latest save.")
    except Exception as e:
        logging.error(f"Failed to update world index: {e}")

# --- INITIALIZATION SEQUENCE ---
# load_configs() populates MODELS_CONFIG, PROVIDERS_CONFIG, NAMES_CONFIG,
# GENERIC_CONFIG, and LOCALIZATION_CONFIG from their JSON files on disk.
# Without this, the /settings endpoint returns empty model/provider lists
# and the UI renders blank.
load_configs()

def _load_event_history_from_log():
    """Re-populate EVENT_HISTORY from the on-disk log so synthesis works after a server restart."""
    global EVENT_HISTORY, EVENT_HISTORY_SET
    log_path = os.path.join(get_campaign_dir(), "logs", "global_events.log")
    if not os.path.exists(log_path):
        log_path = os.path.join(KENSHI_SERVER_DIR, "logs", "global_events.log")
        if not os.path.exists(log_path):
            return
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                bracket = line.find('][')
                if bracket != -1:
                    line = line[bracket + 1:]
                if line and line not in EVENT_HISTORY_SET:
                    EVENT_HISTORY.append(line)
                    EVENT_HISTORY_SET.add(line)
        logging.info(f"Loaded {len(EVENT_HISTORY)} events from global_events.log")
    except Exception as e:
        logging.error(f"Failed to load event history: {e}")


def _clear_campaign_runtime_state():
    """Clear volatile per-campaign state before loading a different campaign."""
    global EVENT_HISTORY, EVENT_HISTORY_SET, _RUMORS_CACHE, _RUMORS_CACHE_MTIME
    clear_live_context_cache()
    _clear_pending_profiles()
    character_gateway.invalidate()  # bust gateway cache on campaign switch
    try:
        import ss_character_registry as _ss_registry
        _ss_registry.clear()
    except Exception:
        pass
    EVENT_HISTORY = []
    EVENT_HISTORY_SET = set()
    _RUMORS_CACHE = []
    _RUMORS_CACHE_MTIME = 0.0
    SYNTHESIS_STATUS["elapsed"] = 0
    with STATE_LOCK:
        LAST_STATE_LOG.clear()
    with THROTTLE_LOCK:
        EVENT_THROTTLE.clear()


def _load_active_campaign_runtime_state():
    """Load the same campaign-scoped runtime state used during startup."""
    load_campaign_config()
    _load_event_history_from_log()
    update_world_index()

def init_server_state():
    """Restore runtime state from INI + campaign files at startup."""
    global ACTIVE_CAMPAIGN, CURRENT_MODEL_KEY, DIALOGUE_HISTORY_LIMIT
    try:
        settings = load_settings()
        ACTIVE_CAMPAIGN = settings.get("current_campaign", "Default")
        CURRENT_MODEL_KEY = settings.get("current_model", "player2-default")
        DIALOGUE_HISTORY_LIMIT = int(settings.get("dialogue_history_limit", 45))
        character_gateway.set_dialogue_limit(DIALOGUE_HISTORY_LIMIT)
        set_current_model_key(CURRENT_MODEL_KEY)
        logging.info(f"INIT: Active Campaign: {ACTIVE_CAMPAIGN}, Model: {CURRENT_MODEL_KEY}")

        # Rewrite the INI so any missing default keys are populated and
        # legacy lowercase keys are normalized to PascalCase.
        persist_current_settings()

        migrate_to_campaigns()
        _load_active_campaign_runtime_state()
    except Exception as e:
        logging.error(f"INIT: Critical state init failure: {e}")
        import traceback
        traceback.print_exc()

init_server_state()

# Align Kayak campaign + registry with the active SS campaign on startup.
# Without this, Kayak may stay on its default campaign until the first manual switch.
if KAYAK_ENABLED and kayak:
    try:
        _ss_campaign_dir = os.path.join(CAMPAIGNS_DIR, ACTIVE_CAMPAIGN)
        kayak.on_save_loaded(ACTIVE_CAMPAIGN, ss_campaign_dir=_ss_campaign_dir)
        if _HAVE_CHARACTER_HANDLER:
            sync_registry_from_kayak(campaign=ACTIVE_CAMPAIGN, kayak_bridge=kayak)
    except Exception as _startup_kayak_sync_err:
        logging.warning(f"KAYAK: Startup campaign sync failed: {_startup_kayak_sync_err}")

HYDRATION_JOBS_DIRNAME = "hydration_jobs"
HYDRATION_JOB_VERSION = 1
HYDRATION_JOB_LOCK = threading.Lock()


def _profile_state(data):
    return str((data or {}).get("_profile_state") or "").strip().lower()


def _profile_is_hydrating(data):
    return _profile_state(data) == "hydrating_intro"


def _hydration_jobs_dir(campaign=None):
    campaign_name = str(campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    hdir = os.path.join(CAMPAIGNS_DIR, campaign_name, HYDRATION_JOBS_DIRNAME)
    os.makedirs(hdir, exist_ok=True)
    return hdir


def _hydration_job_id(name="", storage_id=""):
    raw = str(storage_id or name or "npc").strip() or "npc"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "npc"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe}__{digest}"


def _hydration_job_path(name="", storage_id="", campaign=None, job_id=None):
    jid = str(job_id or _hydration_job_id(name=name, storage_id=storage_id)).strip()
    return os.path.join(_hydration_jobs_dir(campaign), f"{jid}.json")


def _write_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _normalize_hydration_job(job, campaign=None):
    data = copy.deepcopy(job or {})
    profile = copy.deepcopy(data.get("profile_payload") or {})
    name = str(data.get("npc_name") or profile.get("Name") or "").strip()
    storage_id = str(data.get("storage_id") or profile.get("ID") or "").strip()
    campaign_name = str(data.get("campaign") or campaign or ACTIVE_CAMPAIGN or "Default").strip() or "Default"
    history = data.get("conversation_history")
    if history is None:
        history = profile.get("ConversationHistory", []) or []
    history = [
        str(line or "").strip()
        for line in history
        if str(line or "").strip()
    ]

    if name:
        profile["Name"] = name
    if storage_id:
        profile["ID"] = storage_id
    profile["ConversationHistory"] = list(history)
    _mark_profile_hydrating(profile)

    normalized = {
        "version": HYDRATION_JOB_VERSION,
        "job_id": str(data.get("job_id") or _hydration_job_id(name=name, storage_id=storage_id)).strip(),
        "campaign": campaign_name,
        "npc_name": name,
        "storage_id": storage_id,
        "profile_payload": profile,
        "conversation_history": list(history),
        "live_context_snapshot": copy.deepcopy(data.get("live_context_snapshot") or {}),
        "entity_written": bool(data.get("entity_written")),
        "dialogue_written": bool(data.get("dialogue_written")) if history else True,
        "created_at": float(data.get("created_at") or time.time()),
        "updated_at": float(time.time()),
    }
    return normalized


def _load_hydration_job(name="", storage_id="", campaign=None, job_id=None):
    path = _hydration_job_path(name=name, storage_id=storage_id, campaign=campaign, job_id=job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return _normalize_hydration_job(payload, campaign=campaign)
    except Exception as exc:
        logging.warning(f"HYDRATE-JOB: failed to load {path} ({exc})")
        return None


def _write_hydration_job(job, campaign=None):
    normalized = _normalize_hydration_job(job, campaign=campaign)
    path = _hydration_job_path(
        name=normalized.get("npc_name"),
        storage_id=normalized.get("storage_id"),
        campaign=normalized.get("campaign"),
        job_id=normalized.get("job_id"),
    )
    try:
        with HYDRATION_JOB_LOCK:
            _write_json_atomic(path, normalized)
        return normalized
    except Exception as exc:
        logging.warning(
            f"HYDRATE-JOB: failed to persist {normalized.get('npc_name') or normalized.get('storage_id')} ({exc})"
        )
        return None


def _delete_hydration_job(name="", storage_id="", campaign=None, job_id=None):
    path = _hydration_job_path(name=name, storage_id=storage_id, campaign=campaign, job_id=job_id)
    if not os.path.exists(path):
        return False
    try:
        with HYDRATION_JOB_LOCK:
            if os.path.exists(path):
                os.remove(path)
        return True
    except Exception as exc:
        logging.warning(f"HYDRATE-JOB: failed to delete {path} ({exc})")
        return False


def _list_hydration_jobs(campaign=None):
    jobs = []
    hdir = _hydration_jobs_dir(campaign)
    try:
        for entry in sorted(os.listdir(hdir)):
            if not entry.lower().endswith(".json"):
                continue
            payload = _load_hydration_job(campaign=campaign, job_id=os.path.splitext(entry)[0])
            if payload:
                jobs.append(payload)
    except Exception as exc:
        logging.warning(f"HYDRATE-JOB: failed to list jobs for {campaign or ACTIVE_CAMPAIGN} ({exc})")
    return jobs


def _mark_profile_hydrating(data):
    if not data:
        return data
    data["_profile_state"] = "hydrating_intro"
    data["_has_dialogue"] = "0"
    data.pop("_transient", None)
    return data


def _build_hydration_job(clean_name, storage_id, profile_payload, campaign=None, live_context=None, existing_job=None):
    payload = copy.deepcopy(profile_payload or {})
    payload["Name"] = clean_name
    payload["ID"] = storage_id
    history = [
        str(line or "").strip()
        for line in (payload.get("ConversationHistory", []) or [])
        if str(line or "").strip()
    ]
    payload["ConversationHistory"] = list(history)
    _mark_profile_hydrating(payload)

    previous = copy.deepcopy(existing_job or {})
    previous_history = list(previous.get("conversation_history", []) or [])
    job = copy.deepcopy(previous)
    job.update({
        "npc_name": clean_name,
        "storage_id": storage_id,
        "campaign": campaign or ACTIVE_CAMPAIGN,
        "profile_payload": payload,
        "conversation_history": list(history),
        "live_context_snapshot": copy.deepcopy(
            live_context
            or payload.get("_pending_live_context")
            or job.get("live_context_snapshot")
            or {}
        ),
    })
    if "entity_written" not in job:
        job["entity_written"] = False
    if history:
        job["dialogue_written"] = bool(job.get("dialogue_written")) and history == previous_history
    else:
        job["dialogue_written"] = True
    if not job.get("created_at"):
        job["created_at"] = time.time()
    return _normalize_hydration_job(job, campaign=campaign)


def _profile_from_hydration_job(job, ready=False):
    profile = copy.deepcopy((job or {}).get("profile_payload") or {})
    history = [
        str(line or "").strip()
        for line in ((job or {}).get("conversation_history", []) or [])
        if str(line or "").strip()
    ]
    profile["ConversationHistory"] = list(history)
    if (job or {}).get("npc_name"):
        profile["Name"] = job.get("npc_name")
    if (job or {}).get("storage_id"):
        profile["ID"] = job.get("storage_id")
    live_ctx = copy.deepcopy((job or {}).get("live_context_snapshot") or {})
    if live_ctx:
        profile["_pending_live_context"] = live_ctx
    elif "_pending_live_context" in profile:
        profile.pop("_pending_live_context", None)

    if ready:
        _mark_profile_ready(profile)
    else:
        _mark_profile_hydrating(profile)
    return profile


def _sync_hydration_job(profile, name="", storage_id="", campaign=None, live_ctx=None):
    if not profile:
        return None
    clean_name = str(name or profile.get("Name") or "").strip()
    sid = str(storage_id or profile.get("ID") or "").strip()
    if not clean_name or not sid:
        return None
    existing_job = _load_hydration_job(name=clean_name, storage_id=sid, campaign=campaign)
    job = _build_hydration_job(
        clean_name=clean_name,
        storage_id=sid,
        profile_payload=profile,
        campaign=campaign,
        live_context=copy.deepcopy(live_ctx) if isinstance(live_ctx, dict) else None,
        existing_job=existing_job,
    )
    return _write_hydration_job(job, campaign=campaign)


def _sync_hydration_job_from_pending(pending_profile, name="", storage_id="", campaign=None, live_ctx=None):
    if not pending_profile:
        return None
    clean_name = str(name or pending_profile.get("Name") or "").strip()
    sid = str(storage_id or pending_profile.get("ID") or "").strip()
    if not clean_name or not sid:
        return None

    job = _load_hydration_job(name=clean_name, storage_id=sid, campaign=campaign)
    if not job:
        return None

    changed = False
    history = [
        str(line or "").strip()
        for line in (pending_profile.get("ConversationHistory", []) or [])
        if str(line or "").strip()
    ]
    if history != list(job.get("conversation_history", []) or []):
        job["conversation_history"] = list(history)
        job.setdefault("profile_payload", {})["ConversationHistory"] = list(history)
        job["dialogue_written"] = False if history else True
        changed = True

    for key in ("Relation", "SourcePlatoons"):
        if key in pending_profile:
            current = copy.deepcopy(job.get("profile_payload", {}).get(key))
            incoming = copy.deepcopy(pending_profile.get(key))
            if incoming != current:
                job.setdefault("profile_payload", {})[key] = incoming
                changed = True

    merged_live_ctx = copy.deepcopy(
        live_ctx
        or pending_profile.get("_pending_live_context")
        or {}
    )
    if merged_live_ctx and merged_live_ctx != (job.get("live_context_snapshot") or {}):
        job["live_context_snapshot"] = merged_live_ctx
        changed = True

    if not changed:
        return job

    return _write_hydration_job(job, campaign=campaign)


def _run_hydration_job(job, campaign=None, live_ctx=None):
    if not job:
        return None, False

    job = _normalize_hydration_job(job, campaign=campaign)
    name = str(job.get("npc_name") or "").strip()
    storage_id = str(job.get("storage_id") or "").strip()
    campaign_name = str(job.get("campaign") or campaign or ACTIVE_CAMPAIGN).strip() or ACTIVE_CAMPAIGN
    history = list(job.get("conversation_history", []) or [])

    if live_ctx:
        _ctx_copy = copy.deepcopy(live_ctx)
        if _ctx_copy != (job.get("live_context_snapshot") or {}):
            job["live_context_snapshot"] = _ctx_copy
            persisted = _write_hydration_job(job, campaign=campaign_name)
            if not persisted:
                logging.warning(f"HYDRATE-JOB: failed to persist live context for {name}")
                return _profile_from_hydration_job(job, ready=False), False
            job = persisted

    if not history:
        return _profile_from_hydration_job(job, ready=False), False

    hydrating_profile = _profile_from_hydration_job(job, ready=False)
    if not job.get("entity_written"):
        _entity_ok = character_gateway.write_entity_only(hydrating_profile, campaign_name, reason="hydration")
        if not _entity_ok:
            logging.warning(f"HYDRATE: entity write failed for {name}; job preserved")
            return hydrating_profile, False
        job["entity_written"] = True
        persisted = _write_hydration_job(job, campaign=campaign_name)
        if not persisted:
            logging.warning(f"HYDRATE-JOB: failed to persist entity step for {name}")
            return hydrating_profile, False
        job = persisted

    if not job.get("dialogue_written"):
        _dialogue_ok = character_gateway.write_dialogue_only(
            name,
            storage_id,
            history,
            campaign=campaign_name,
        )
        if not _dialogue_ok:
            logging.warning(f"HYDRATE: dialogue write failed for {name}; job preserved")
            return _profile_from_hydration_job(job, ready=False), False
        job["dialogue_written"] = True
        persisted = _write_hydration_job(job, campaign=campaign_name)
        if not persisted:
            logging.warning(f"HYDRATE-JOB: failed to persist dialogue step for {name}")
            return _profile_from_hydration_job(job, ready=False), False
        job = persisted

    ready_profile = _profile_from_hydration_job(job, ready=True)
    _promote_ok = character_gateway.write_entity_only(ready_profile, campaign_name, reason="hydration_promote")
    if not _promote_ok:
        logging.warning(f"HYDRATE: ready promotion write failed for {name}; job preserved")
        return hydrating_profile, False

    if KAYAK_ENABLED and _HAVE_CHARACTER_HANDLER:
        try:
            _gen_pid = create_from_profile(
                ready_profile,
                npc_name=name,
                campaign=campaign_name,
                kayak_bridge=kayak,
            )
            if _gen_pid:
                ready_profile["persistent_id"] = _gen_pid
        except Exception as _hydrate_registry_err:
            logging.warning(f"HYDRATE: registry sync failed for {name} ({_hydrate_registry_err})")

    _delete_hydration_job(
        name=name,
        storage_id=storage_id,
        campaign=campaign_name,
        job_id=job.get("job_id"),
    )
    return ready_profile, True


def _resume_hydration(name, storage_id, campaign, live_ctx=None):
    job = _load_hydration_job(name=name, storage_id=storage_id, campaign=campaign)
    if not job:
        return None
    result, _ = _run_hydration_job(job, campaign=campaign, live_ctx=live_ctx)
    return result


def _rename_hydration_job(old_name, old_storage_id, new_name, new_storage_id, campaign=None, profile=None, live_ctx=None):
    job = _load_hydration_job(name=old_name, storage_id=old_storage_id, campaign=campaign)
    if not job:
        return None
    moved = copy.deepcopy(job)
    moved.pop("job_id", None)
    moved["npc_name"] = str(new_name or moved.get("npc_name") or "").strip()
    moved["storage_id"] = str(new_storage_id or moved.get("storage_id") or "").strip()
    if profile:
        moved["profile_payload"] = copy.deepcopy(profile)
    if live_ctx:
        moved["live_context_snapshot"] = copy.deepcopy(live_ctx)
    persisted = _write_hydration_job(moved, campaign=campaign)
    if persisted:
        _delete_hydration_job(
            name=old_name,
            storage_id=old_storage_id,
            campaign=campaign,
            job_id=job.get("job_id"),
        )
    return persisted


def _startup_resume_hydrations():
    if not character_gateway.available:
        return

    resumed = 0
    for job in _list_hydration_jobs(ACTIVE_CAMPAIGN):
        if not list(job.get("conversation_history", []) or []):
            continue
        result = _resume_hydration(
            job.get("npc_name", ""),
            job.get("storage_id", ""),
            ACTIVE_CAMPAIGN,
        )
        if result and not _profile_is_hydrating(result):
            resumed += 1

    if resumed:
        logging.info(f"STARTUP-HYDRATE: resumed {resumed} incomplete hydration(s)")

if KAYAK_ENABLED and kayak:
    try:
        _startup_resume_hydrations()
    except Exception as _startup_hydrate_err:
        logging.warning(f"STARTUP-HYDRATE: scan failed: {_startup_hydrate_err}")

# Initial scan
update_world_index()

# Requirement: "Character Initialization Attachment"
# Fulfill by ensuring registry files exist for all known characters
def populate_initial_registry():
    registry_dir = os.path.join(get_campaign_dir(), "sentient_sands_registry")
    if not os.path.exists(registry_dir):
        os.makedirs(registry_dir)
    
    for name, platoons in WORLD_INDEX.items():
        clean_name = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
        if not clean_name: continue
        reg_file = os.path.join(registry_dir, f"{clean_name}_init.txt")
        if not os.path.exists(reg_file):
             with open(reg_file, "w", encoding="utf-8") as f:
                 f.write(f"Registry: {name} initialized. Location: {platoons[0]}\n")

    removed = cleanup_fragment_registry_files(registry_dir, WORLD_INDEX)
    if removed:
        logging.info(f"Registry cleanup removed {removed} split fragments from {registry_dir}")

populate_initial_registry()

# Characters directory is managed by load_campaign_config()
# Do not re-assign here.

# call_llm lives in ss_llm — imported above

CANON_CHARACTERS_PATH = os.path.join(SCRIPT_DIR, "..", "config", "canon_characters.json")
CANON_CHARACTERS = {}

def load_canon_characters():
    global CANON_CHARACTERS
    if os.path.exists(CANON_CHARACTERS_PATH):
        try:
            with open(CANON_CHARACTERS_PATH, "r") as f:
                data = json.load(f)
                for char in data:
                    CANON_CHARACTERS[char["Name"].lower()] = char
            logging.info(f"Loaded {len(CANON_CHARACTERS)} canon characters.")
        except Exception as e:
            logging.error(f"Failed to load canon_characters.json: {e}")

load_canon_characters()

def _est_prompt_tokens(text):
    """Cheap, conservative estimate used only for prompt logging/guardrails."""
    return len(str(text or "")) * 2 // 7


def log_prompt_snapshot(prompt_type, prompt=None, messages=None, metadata=None):
    """
    Append a human-readable prompt snapshot to server/logs/<prompt_type>.log.
    Keeps logging local to Sentient Sands so merged Kayak/native paths can be
    compared side-by-side without relying on Kayak's own logs.
    """
    try:
        log_dir = os.path.join(KENSHI_SERVER_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)

        safe_type = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(prompt_type or "prompt")).strip("._")
        if not safe_type:
            safe_type = "prompt"
        log_path = os.path.join(log_dir, f"{safe_type}.log")

        parts = [f"TIMESTAMP: {time.ctime()}"]
        if metadata:
            for key, value in metadata.items():
                if value is None:
                    continue
                parts.append(f"{key}: {value}")

        if messages is not None:
            prompt_text_parts = []
            for idx, msg in enumerate(messages, start=1):
                role = str((msg or {}).get("role", "unknown")).upper()
                content = str((msg or {}).get("content", "") or "")
                prompt_text_parts.append(f"[MESSAGE {idx} | {role}]\n{content}")
            rendered = "\n\n".join(prompt_text_parts)
        else:
            rendered = str(prompt or "")

        parts.append(f"EST_TOKENS: ~{_est_prompt_tokens(rendered)}")
        parts.append("PROMPT:")
        parts.append(rendered)

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("\n".join(parts))
            f.write("\n")
    except Exception as e:
        logging.debug(f"Prompt snapshot logging failed for {prompt_type}: {e}")

def generate_character_profile(name, context=""):
    lower_name = name.lower()
    if "your squad" in lower_name or "squad" == lower_name:
        player_faction = get_effective_player_context().get('faction', 'Nameless')
        return {
            "Personality": "A collective of your loyal companions, each with their own views but united in purpose. They are loyal to you and the squad's goals.",
            "Backstory": f"You have traveled together as members of the {player_faction} through the harsh lands of Kenshi, surviving against all odds.",
            "SpeechQuirks": "Speaks as a representative of the group, sometimes mentioning others in the squad.",
            "Race": "Mixed",
            "Faction": player_faction,
            "Sex": "Mixed"
        }

    if lower_name in CANON_CHARACTERS:
        logging.info(f"Found canon match for {name}")
        return CANON_CHARACTERS[lower_name]

    # Extract race/faction from request-local context or the live cache.
    _, live_ctx = resolve_live_context(name=name, context=context)
    live_ctx = live_ctx or {}
    
    race = "Unknown"
    gender = "Unknown"
    faction = "Unknown"
    origin_faction = "Unknown"
    job = "None"

    # Try context first
    ctx_data = {}
    if isinstance(context, dict):
        ctx_data = context
    elif isinstance(context, str) and context.strip().startswith('{'):
        try:
            ctx_data = json.loads(context)
        except: pass
        
    if ctx_data:
        race = ctx_data.get('race', race)
        gender = ctx_data.get('gender', gender)
        faction = ctx_data.get('faction', faction)
        if faction == "Unknown":
            faction = ctx_data.get('factionID', "Unknown")
        origin_faction = ctx_data.get('origin_faction', "Unknown")
        job = ctx_data.get('job', "None")
    
    # Fallback to LIVE_CONTEXTS if still unknown
    if race == "Unknown": race = live_ctx.get('race', 'Unknown')
    if gender == "Unknown": gender = live_ctx.get('gender', 'Unknown')
    if faction == "Unknown": 
        faction = live_ctx.get('faction', 'Unknown')
        if faction == "Unknown":
            faction = live_ctx.get('factionID', "Unknown")
    
    if origin_faction == "Unknown": origin_faction = live_ctx.get('origin_faction', 'Unknown')
    if job == "None": job = live_ctx.get('job', 'None')
    persona_category = get_persona_category(
        race,
        faction,
        name=name,
        source="profile_generation",
    )
    
    # RELAXED CONSTRAINTS: Only skip if we truly have nothing or the name is generic.
    # Modded factions often fail to report pretty names through standard hooks.
    if name in ("Unknown", "Someone", "Unknown Entity"):
        logging.info(f"Skipping profile: Name is {name}.")
        return None


    logging.info(f"Generating rich profile for {name} ({gender} {race}, Base Faction: {origin_faction}, Job: {job})...")
    
    template = load_prompt_component("prompt_profile_generation.txt", """You are an expert on Kenshi lore.
Task: Generate a character profile for the NPC named "{name}".
SEX: {gender}
RACE: {race}
ORIGIN FACTION: {origin_faction}
CURRENT FACTION: {faction}
JOB: {job}
DATA: {context}

CRITICAL RULES:
1. CANON FIRST: If "{name}" is a known Kenshi character (e.g. Beep, Holy Lord Phoenix, Cat-Lon), use exact canon lore.
2. NON-CANON: If generic (e.g. "Dust Bandit", "Shop Guard"), create a grounded profile fitting the setting.
3. PERSONALITY: The character MUST speak and behave according to their sex ({gender}) and race ({race}). 
4. OUTPUT: JSON only with keys: "Personality", "Backstory", "SpeechQuirks".
""")
    f_info = get_faction_info(faction)
    o_info = get_faction_info(origin_faction)

    prompt = template.format(name=name, gender=gender, race=race, faction=f_info, origin_faction=o_info, job=job, context=context)
    
    # Apply language instruction for profile generation
    settings = load_settings()
    language = settings.get("language", "English")
    if language and language.lower() != "english":
        prompt += f"\nLANGUAGE: The JSON values ('Personality', 'Backstory', 'SpeechQuirks') MUST be written entirely in {language}. Do not use English.\n"
    
    # added by Pineaxe v04 - use Kayak biography prompt if available
    _bio_sys = None
    if KAYAK_ENABLED:
        try:
            _npc_facts = (
                f"Name: {name}\nRace: {race}\nSex: {gender}\n"
                f"Faction: {faction}\nOrigin Faction: {origin_faction}\nJob: {job}"
            )
            _bio_sys = kayak.build_biography_prompt(
                _npc_facts,
                campaign=ACTIVE_CAMPAIGN,
                race=race,
                persona_category=persona_category,
            )
            if _bio_sys:
                logging.info(f"KAYAK: Using Kayak biography prompt for {name}")
        except Exception as _kbe:
            logging.warning(f"KAYAK: build_biography_prompt failed ({_kbe}) - using native prompt")
    if _bio_sys:
        messages = [
            {"role": "system", "content": _bio_sys},
            {"role": "user",   "content": f"Generate a profile for {name}."}
        ]
    else:
        messages = [{"role": "user", "content": prompt}]
    log_prompt_snapshot("profile_generation", messages=messages, metadata={"npc": name, "campaign": ACTIVE_CAMPAIGN, "kayak_prompt": bool(_bio_sys)})
    response_text = call_llm(messages, max_tokens=int(load_settings().get("profile_max_tokens", 1200)), temperature=float(load_settings().get("profile_temperature", 0.7)))  # added by Pineaxe v07 - configurable via settings.json

    if response_text:
        try:
            result = robust_json_parse(response_text)
            if result:
                # Add race/faction to result for get_character_data
                result["Race"] = race
                result["Faction"] = faction
                result["OriginFaction"] = origin_faction
                result["Job"] = job
                result["Sex"] = gender
                result["_persona_category"] = persona_category
                result["Traits"] = generate_npc_traits(faction, race, origin_faction)
                return result
        except Exception as e:
            logging.error(f"Failed to parse generated profile: {e}")
            
    return {
        "Personality": "A weary wanderer.",
        "Backstory": "Trying to survive in the harsh desert.",
        "SpeechQuirks": "None.",
        "Race": race,
        "Faction": faction,
        "OriginFaction": origin_faction,
        "Job": job,
        "Sex": gender,
        "_persona_category": persona_category,
        "Traits": generate_npc_traits(faction, race, origin_faction)
    }

def _merge_conversation_history(existing_lines, incoming_lines):
    existing = [
        str(line).strip()
        for line in (existing_lines or [])
        if str(line or "").strip()
    ]
    incoming = [
        str(line).strip()
        for line in (incoming_lines or [])
        if str(line or "").strip()
    ]

    if not existing:
        return list(incoming)
    if not incoming:
        return list(existing)

    # Fast paths for the common "pending history extends current history" cases.
    if len(incoming) >= len(existing) and incoming[:len(existing)] == existing:
        return list(incoming)
    if len(existing) >= len(incoming) and existing[:len(incoming)] == incoming:
        return list(existing)

    max_overlap = min(len(existing), len(incoming))
    for overlap in range(max_overlap, 0, -1):
        if existing[-overlap:] == incoming[:overlap]:
            return existing + incoming[overlap:]
        if incoming[-overlap:] == existing[:overlap]:
            return incoming + existing[overlap:]

    # Fallback: preserve order and avoid duplicating identical full lines.
    merged = list(existing)
    for line in incoming:
        if line not in merged:
            merged.append(line)
    return merged


def generate_batch_profiles(npc_list):
    """Lump multiple NPC profile generations into a single LLM call."""
    if not npc_list: return
    
    # Filter out any NPCs that don't have all three required fields.
    # These will be deferred until we have full context from the game.
    complete = []
    for npc in npc_list:
        name = npc.get('name', 'Unknown')
        race = npc.get('race', 'Unknown')
        gender = npc.get('gender', 'Unknown')
        faction = npc.get('faction', 'Unknown')
        missing = [k for k, v in {"race": race, "gender": gender, "faction": faction}.items() if v in ("Unknown", None, "")]
        if missing:
            logging.info(f"BATCH: Skipping {name} \u2014 missing {', '.join(missing)}, will generate on next full context.")
        else:
            complete.append(npc)

    if not complete:
        logging.info("BATCH: No complete NPC data available, deferring all profiles.")
        return
    
    logging.info(f"BATCH: Generating {len(complete)} profiles ({len(npc_list) - len(complete)} deferred)...")

    template = load_prompt_component("prompt_batch_profile_generation.txt", """You are an expert on Kenshi lore.
Task: Generate character profiles for several NPCs at once.

NPCS TO GENERATE:
{desc_str}

CRITICAL RULES:
1. CANON FIRST: If a name is a known Kenshi character (e.g. Beep, Holy Lord Phoenix), use exact canon lore.
2. NON-CANON: Generate grounded, cynical, or weary profiles fitting the harsh Kenshi setting.
3. OUTPUT: Return a JSON object where each key is the NPC's Name, and the value is an object with: "Personality", "Backstory", "SpeechQuirks".
""")

    settings = load_settings()
    language = settings.get("language", "English")

    # Split into chunks of 10 so no single LLM call exceeds ~1500 tokens of output.
    # Each profile takes ~75-100 tokens; 20+ NPCs (large bars) would truncate mid-JSON otherwise.
    _BATCH_CHUNK_SIZE = 3
    all_results = {}
    for _chunk_i in range(0, len(complete), _BATCH_CHUNK_SIZE):
        _chunk = complete[_chunk_i:_chunk_i + _BATCH_CHUNK_SIZE]
        _chunk_descs = "\n".join(
            f"- Name: {n.get('name','Unknown')}, Sex: {n.get('gender','Unknown')}, "
            f"Race: {n.get('race','Unknown')}, Faction: {get_faction_info(n.get('faction','Unknown'))}"
            for n in _chunk
        )
        _chunk_prompt = template.format(desc_str=_chunk_descs)
        if language and language.lower() != "english":
            _chunk_prompt += f"\nLANGUAGE: All generated profile values ('Personality', 'Backstory', 'SpeechQuirks') MUST be written entirely in {language}. Do not use English for the values.\n"
        _chunk_messages = [{"role": "user", "content": _chunk_prompt}]
        _chunk_num = _chunk_i // _BATCH_CHUNK_SIZE + 1
        _total_chunks = (len(complete) + _BATCH_CHUNK_SIZE - 1) // _BATCH_CHUNK_SIZE
        logging.info(f"BATCH: Chunk {_chunk_num}/{_total_chunks} ({len(_chunk)} NPCs)...")
        _chunk_max = min(4000, max(1500, len(_chunk) * 800))
        log_prompt_snapshot("batch_profile_generation", messages=_chunk_messages, metadata={"chunk": f"{_chunk_num}/{_total_chunks}", "npc_count": len(_chunk), "campaign": ACTIVE_CAMPAIGN})
        _chunk_text = call_llm(_chunk_messages, max_tokens=_chunk_max, temperature=0.7)
        if _chunk_text:
            try:
                _chunk_results = robust_json_parse(_chunk_text)
                if _chunk_results:
                    # Detect single-NPC unwrapped response: LLM returned {Personality:..., Backstory:..., SpeechQuirks:...}
                    # instead of {NPC_Name: {Personality:..., ...}}. Re-wrap with the NPC's clean name.
                    if (len(_chunk) == 1
                            and "Personality" in _chunk_results
                            and "Backstory" in _chunk_results
                            and isinstance(_chunk_results.get("Personality"), str)):
                        _only_ctx = _parse_context_dict(_chunk[0], fallback_name=_chunk[0].get("name", "Unknown"))
                        _only_name = (_only_ctx.get("name") or _chunk[0].get("name", "Unknown")).split("|")[0]
                        logging.info(f"BATCH: Re-wrapping unwrapped single-NPC response for '{_only_name}'")
                        _chunk_results = {_only_name: _chunk_results}
                    all_results.update(_chunk_results)
            except Exception as _e:
                logging.error(f"BATCH: Failed to parse chunk {_chunk_num}: {_e}")

    if all_results:
        try:
            batch_results = all_results
            processed_data = []  # Collect for Kayak batch write
            
            for npc in npc_list:
                npc_ctx = _parse_context_dict(npc, fallback_name=npc.get('name', 'Unknown'))
                raw_name = str(npc_ctx.get('name', npc.get('name', 'Unknown'))).strip()
                clean_name = raw_name.split('|')[0].strip() if '|' in raw_name else raw_name
                gender = npc_ctx.get('gender', npc.get('gender', 'Neutral'))

                # Try to find profile by exact clean name, raw name, or case-insensitive match
                profile = batch_results.get(clean_name) or batch_results.get(raw_name)

                if not profile:
                    # Case-insensitive and pipe-resilient fallback
                    clean_low = clean_name.lower()
                    raw_low = raw_name.lower()
                    for k, v in batch_results.items():
                        k_low = k.lower()
                        # Strip ID from LLM key if it included it
                        k_clean_low = k_low.split('|')[0].strip() if '|' in k_low else k_low.strip()

                        if k_low == clean_low or k_low == raw_low or k_clean_low == clean_low:
                            profile = v
                            break

                if not profile:
                    logging.warning(
                        f"BATCH: No match for clean='{clean_name}' raw='{raw_name}' "
                        f"LLM keys={list(batch_results.keys())}"
                    )

                if profile:
                    _batch_faction = npc_ctx.get('faction') or npc_ctx.get('Faction') or 'Unknown'
                    storage_id = npc_ctx.get("storage_id") or make_storage_id(clean_name, _batch_faction, context=npc_ctx)
                    _batch_race = npc_ctx.get('race', 'Unknown')
                    _batch_origin = npc_ctx.get('origin_faction', 'Unknown')
                    pending_key, pending_existing = _pending_lookup(
                        name=clean_name,
                        context=npc_ctx,
                        explicit_id=(
                            npc_ctx.get("persistent_id")
                            or npc_ctx.get("runtime_id")
                            or npc_ctx.get("id")
                            or storage_id
                        ),
                        live_ctx=npc_ctx,
                        storage_id=storage_id,
                    )
                    data = {
                        "ID": storage_id,
                        "Name": clean_name,
                        "OriginalName": clean_name,
                        "Race": _batch_race,
                        "Sex": gender or 'Unknown',
                        "Faction": _batch_faction,
                        "OriginFaction": _batch_origin,
                        "Job": npc_ctx.get('job', npc.get('job', 'None')),
                        "Personality": profile.get("Personality", "A weary traveler."),
                        "Backstory": profile.get("Backstory", "Trying to survive in the harsh desert."),
                        "SpeechQuirks": profile.get("SpeechQuirks", "None."),
                        "Traits": generate_npc_traits(_batch_faction, _batch_race, _batch_origin),
                        "ConversationHistory": [],
                        "Relation": 0
                    }
                    existing = character_gateway.read(clean_name, storage_id, ACTIVE_CAMPAIGN)
                    if existing:
                        data["ConversationHistory"] = list(existing.get("ConversationHistory", []))
                        data["Relation"] = existing.get("Relation", 0)
                        if existing.get("SourcePlatoons"):
                            data["SourcePlatoons"] = existing["SourcePlatoons"]
                    if pending_existing:
                        merged_history = _merge_conversation_history(
                            data.get("ConversationHistory", []),
                            pending_existing.get("ConversationHistory", []),
                        )
                        if merged_history:
                            data["ConversationHistory"] = merged_history[-DIALOGUE_HISTORY_LIMIT:]
                        try:
                            data["Relation"] = int(
                                pending_existing.get("Relation", data.get("Relation", 0)) or 0
                            )
                        except (TypeError, ValueError):
                            pass
                        if pending_existing.get("SourcePlatoons") and not data.get("SourcePlatoons"):
                            data["SourcePlatoons"] = pending_existing["SourcePlatoons"]

                    _mark_profile_ready(data)
                    
                    # Gate: Only block feral NPCs from Kayak persistence.
                    # All other named NPCs (even generics) need their bios saved
                    # to support ambient, overhear, and yell features.
                    _persona_cat = get_persona_category(_batch_race, _batch_faction, name=clean_name, source="batch_generation")
                    data["_persona_category"] = _persona_cat
                    _should_save = _persona_cat != "feral"

                    if _should_save:
                        character_gateway.write(data, ACTIVE_CAMPAIGN, reason="batch_generation")
                        processed_data.append(data)  # Collect for Kayak batch
                        if pending_existing:
                            _pending_discard(
                                name=clean_name,
                                context=npc_ctx,
                                explicit_id=(
                                    npc_ctx.get("persistent_id")
                                    or npc_ctx.get("runtime_id")
                                    or npc_ctx.get("id")
                                    or storage_id
                                ),
                                live_ctx=npc_ctx,
                                storage_id=storage_id,
                                profile=pending_existing,
                                key=(pending_existing or {}).get("_pending_key") or pending_key,
                            )
                        logging.info(f"BATCH: Saved profile for {clean_name} (ID: {storage_id})")
                    else:
                        logging.info(f"BATCH: Skipped saving profile for {clean_name} (ID: {storage_id}) [Persona={_persona_cat}, feral blocked]")
            
            # Write all generated profiles to Kayak in batch (parallel)
            if KAYAK_ENABLED and _HAVE_CHARACTER_HANDLER and processed_data:
                try:
                    batch_results = create_batch(
                        processed_data,
                        campaign=ACTIVE_CAMPAIGN,
                        kayak_bridge=kayak
                    )
                    logging.info(f"BATCH: Wrote {len(batch_results)} NPCs to Kayak")
                except Exception as _batch_kayak_e:
                    logging.warning(f"BATCH: Kayak batch write failed ({_batch_kayak_e}) - profiles are in native system")
        except Exception as e:
            logging.error(f"BATCH: Failed to parse batch profiles: {e}")

def queue_batch_profile_generation(npc_list):
    """Queue profile generation in the background and mark IDs as in-progress up front."""
    if not npc_list:
        return 0

    queued = []
    with PROGRESS_LOCK:
        for npc in npc_list:
            if not isinstance(npc, dict):
                continue

            ctx = _parse_context_dict(npc, fallback_name=npc.get("name", "Unknown"))
            raw_name = ctx.get("name", npc.get("name", "Unknown"))
            clean_name = raw_name.split('|')[0] if '|' in raw_name else raw_name
            faction = ctx.get("faction") or ctx.get("Faction") or npc.get("faction") or npc.get("Faction") or ""
            storage_id = ctx.get("storage_id") or make_storage_id(clean_name, faction, context=ctx)

            if storage_id in PROFILES_IN_PROGRESS:
                continue

            PROFILES_IN_PROGRESS.add(storage_id)
            ctx["storage_id"] = storage_id
            queued.append(ctx)

    if not queued:
        return 0
    if direct_chat_active():
        defer_profile_batch(queued)
        logging.info(f"BATCH: Deferred {len(queued)} profiles while direct chat is active.")
        return len(queued)

    return _launch_batch_profile_generation(queued)


def defer_profile_batch(npc_list):
    """Hold profile generation requests until direct chat is idle."""
    if not npc_list:
        return 0
    added = 0
    with PROGRESS_LOCK:
        for npc in npc_list:
            if not isinstance(npc, dict):
                continue
            storage_id = str(npc.get("storage_id") or "").strip()
            if not storage_id:
                continue
            DEFERRED_PROFILE_QUEUE[storage_id] = npc
            added += 1
    return added


def _launch_batch_profile_generation(npc_list):
    """Start batch generation on a background thread."""
    if not npc_list:
        return 0

    def _runner(batch):
        try:
            generate_batch_profiles(batch)
        except Exception as e:
            logging.error(f"BATCH: Background generation failed: {e}")
        finally:
            with PROGRESS_LOCK:
                for npc in batch:
                    sid = str((npc or {}).get("storage_id") or "").strip()
                    if sid:
                        PROFILES_IN_PROGRESS.discard(sid)

    threading.Thread(target=_runner, args=(list(npc_list),), daemon=True).start()
    return len(npc_list)


def flush_deferred_profile_batches():
    """Launch deferred profile work once direct chat is no longer active."""
    if direct_chat_active():
        return 0
    with PROGRESS_LOCK:
        if not DEFERRED_PROFILE_QUEUE:
            return 0
        queued = list(DEFERRED_PROFILE_QUEUE.values())
        DEFERRED_PROFILE_QUEUE.clear()
    return _launch_batch_profile_generation(queued)

def _string_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _pending_clone(data):
    return copy.deepcopy(data) if data else None


def _pending_candidate_keys(
    name="",
    context=None,
    explicit_id=None,
    live_ctx=None,
    storage_id=None,
    profile=None,
):
    ctx = _parse_context_dict(context, fallback_name=name)
    live = live_ctx if isinstance(live_ctx, dict) else {}
    current = profile if isinstance(profile, dict) else {}
    keys = []

    def _add(candidate, *, strong_only=False):
        text = str(candidate or "").strip()
        if not text:
            return
        if strong_only and not _is_strong_uid(text):
            return
        if text not in keys:
            keys.append(text)

    _add(current.get("_pending_key"))
    for candidate in (
        ctx.get("runtime_id"),
        ctx.get("id"),
        live.get("runtime_id"),
        live.get("id"),
    ):
        _add(candidate)

    for candidate in (
        ctx.get("persistent_id"),
        live.get("persistent_id"),
        current.get("persistent_id"),
    ):
        _add(candidate, strong_only=True)

    _add(explicit_id)
    for candidate in (
        storage_id,
        ctx.get("storage_id"),
        ctx.get("ID"),
        live.get("storage_id"),
        live.get("ID"),
        current.get("ID"),
    ):
        _add(candidate)

    for candidate in (
        name,
        ctx.get("name"),
        live.get("name"),
        current.get("Name"),
    ):
        _add(candidate)

    for alias in current.get("_pending_aliases", []) or []:
        _add(alias)

    return keys


def _pending_lookup(
    name="",
    context=None,
    explicit_id=None,
    live_ctx=None,
    storage_id=None,
    profile=None,
):
    candidate_keys = _pending_candidate_keys(
        name=name,
        context=context,
        explicit_id=explicit_id,
        live_ctx=live_ctx,
        storage_id=storage_id,
        profile=profile,
    )
    with PENDING_LOCK:
        for key in candidate_keys:
            stored = PENDING_PROFILES.get(key)
            if stored:
                return key, _pending_clone(stored)

        lowered = {str(x).strip().lower() for x in candidate_keys if str(x or "").strip()}
        if lowered:
            for stored_key, stored in PENDING_PROFILES.items():
                aliases = set(stored.get("_pending_aliases", []) or [])
                aliases.add(stored.get("Name", ""))
                aliases.add(stored.get("ID", ""))
                aliases.add(stored.get("_pending_key", ""))
                if any(str(alias or "").strip().lower() in lowered for alias in aliases if alias):
                    return stored_key, _pending_clone(stored)

    return None, None


def _pending_store(
    profile,
    name="",
    context=None,
    explicit_id=None,
    live_ctx=None,
    storage_id=None,
    replace_keys=None,
):
    if not profile:
        return None

    incoming = _pending_clone(profile) or {}
    existing_key, existing = _pending_lookup(
        name=name or incoming.get("Name", ""),
        context=context,
        explicit_id=explicit_id,
        live_ctx=live_ctx,
        storage_id=storage_id,
        profile=incoming,
    )

    chosen_key = (
        str(incoming.get("_pending_key") or "").strip()
        or str(existing_key or "").strip()
    )
    if not chosen_key:
        candidate_keys = _pending_candidate_keys(
            name=name or incoming.get("Name", ""),
            context=context,
            explicit_id=explicit_id,
            live_ctx=live_ctx,
            storage_id=storage_id,
            profile=incoming,
        )
        chosen_key = candidate_keys[0] if candidate_keys else ""
    if not chosen_key:
        return _pending_clone(incoming)

    aliases = set((existing or {}).get("_pending_aliases", []) or [])
    aliases.update(incoming.get("_pending_aliases", []) or [])
    for alias in (
        name,
        incoming.get("Name"),
        (existing or {}).get("Name"),
    ):
        text = str(alias or "").strip()
        if text:
            aliases.add(text)

    incoming["_pending_key"] = chosen_key
    if aliases:
        incoming["_pending_aliases"] = sorted(aliases)
    else:
        incoming.pop("_pending_aliases", None)
    incoming["_profile_state"] = "pending_intro"
    incoming["_transient"] = True
    incoming["_has_dialogue"] = "1" if incoming.get("ConversationHistory") else str(incoming.get("_has_dialogue") or "0")
    carry_live_context = {}
    if isinstance(live_ctx, dict) and live_ctx:
        carry_live_context = copy.deepcopy(live_ctx)
    elif isinstance(context, dict) and context:
        carry_live_context = copy.deepcopy(context)
    if carry_live_context:
        incoming["_pending_live_context"] = carry_live_context

    drop_keys = set(str(k).strip() for k in (replace_keys or []) if str(k or "").strip())
    if existing_key:
        drop_keys.add(str(existing_key).strip())

    with PENDING_LOCK:
        for key in list(drop_keys):
            if key and key != chosen_key:
                PENDING_PROFILES.pop(key, None)
        PENDING_PROFILES[chosen_key] = _pending_clone(incoming)

    _sync_hydration_job_from_pending(
        incoming,
        name=name or incoming.get("Name", ""),
        storage_id=storage_id or incoming.get("ID", ""),
        campaign=ACTIVE_CAMPAIGN,
        live_ctx=copy.deepcopy(carry_live_context),
    )

    return _pending_clone(incoming)


def _pending_discard(
    name="",
    context=None,
    explicit_id=None,
    live_ctx=None,
    storage_id=None,
    profile=None,
    key=None,
):
    drop_keys = set()
    if key:
        text = str(key).strip()
        if text:
            drop_keys.add(text)

    found_key, _ = _pending_lookup(
        name=name,
        context=context,
        explicit_id=explicit_id,
        live_ctx=live_ctx,
        storage_id=storage_id,
        profile=profile,
    )
    if found_key:
        drop_keys.add(str(found_key).strip())

    if not drop_keys:
        return False

    removed = False
    with PENDING_LOCK:
        for candidate in drop_keys:
            if candidate and candidate in PENDING_PROFILES:
                PENDING_PROFILES.pop(candidate, None)
                removed = True
    return removed


def _clear_pending_profiles():
    with PENDING_LOCK:
        PENDING_PROFILES.clear()


def _mark_profile_ready(data):
    if not data:
        return data
    history = list(data.get("ConversationHistory", []) or [])
    data["_profile_state"] = "ready"
    data["_has_dialogue"] = "1" if history else "0"
    data.pop("_transient", None)
    data.pop("_pending_key", None)
    data.pop("_pending_aliases", None)
    data.pop("_pending_live_context", None)
    return data


def _build_pending_profile(name, storage_id, ctx_data=None, live_ctx=None, existing=None):
    ctx = ctx_data if isinstance(ctx_data, dict) else {}
    live = live_ctx if isinstance(live_ctx, dict) else {}
    current = dict(existing or {})

    faction = ctx.get("faction") or live.get("faction") or current.get("Faction") or "Unknown"
    race = ctx.get("race") or live.get("race") or current.get("Race") or "Unknown"
    sex = ctx.get("gender") or live.get("gender") or current.get("Sex") or "Unknown"
    origin_faction = ctx.get("origin_faction") or live.get("origin_faction") or current.get("OriginFaction") or faction
    job = ctx.get("job") or live.get("job") or current.get("Job") or "None"
    history = list(current.get("ConversationHistory", []) or [])
    relation = current.get("Relation", 0)
    try:
        relation = int(relation)
    except (TypeError, ValueError):
        relation = 0

    pending = {
        "ID": storage_id,
        "Name": name,
        "Race": str(race or "Unknown").strip() or "Unknown",
        "Sex": str(sex or "Unknown").strip() or "Unknown",
        "Faction": str(faction or "Unknown").strip() or "Unknown",
        "OriginFaction": str(origin_faction or faction or "Unknown").strip() or "Unknown",
        "Job": str(job or "None").strip() or "None",
        "Personality": "",
        "Backstory": "",
        "SpeechQuirks": "",
        "ConversationHistory": history,
        "Relation": relation,
        "Traits": current.get("Traits") or generate_npc_traits(
            str(faction or "Unknown"),
            str(race or "Unknown"),
            str(origin_faction or faction or "Unknown"),
        ),
        "_profile_state": "pending_intro",
        "_has_dialogue": "1" if history else "0",
        "_transient": True,
    }
    if current.get("_original_name"):
        pending["_original_name"] = current["_original_name"]
    if current.get("_pending_key"):
        pending["_pending_key"] = current["_pending_key"]
    if current.get("_pending_aliases"):
        pending["_pending_aliases"] = list(current["_pending_aliases"])
    if current.get("_pending_live_context"):
        pending["_pending_live_context"] = copy.deepcopy(current["_pending_live_context"])
    return pending


def _pending_observation_lines(char_data, context=""):
    ctx = _parse_context_dict(context, fallback_name=char_data.get("Name", "Someone"))
    env = ctx.get("environment") or {}
    observed = []

    for label, value in (
        ("Race", char_data.get("Race")),
        ("Faction", char_data.get("Faction")),
        ("Origin", char_data.get("OriginFaction")),
        ("Sex", char_data.get("Sex")),
        ("Job", char_data.get("Job")),
    ):
        value = str(value or "").strip()
        if value and value not in {"Unknown", "None"}:
            observed.append(f"{label}: {value}")

    town = str(env.get("town_name") or env.get("town") or ctx.get("town") or "").strip()
    region = str(env.get("region") or ctx.get("region") or "").strip()
    building = str(env.get("building_name") or env.get("building") or ctx.get("building") or "").strip()

    if town:
        observed.append(f"Town: {town}")
    if region:
        observed.append(f"Region: {region}")
    if building:
        observed.append(f"Building: {building}")

    return observed


def _campaign_npcs_root(campaign=None):
    return _npc_category_root(campaign, "campaign_npcs")


def _npc_category_root(campaign=None, category="campaign_npcs"):
    category_name = str(category or "campaign_npcs").strip().lower()
    if category_name not in {"campaign_npcs", "base_npcs"}:
        category_name = "campaign_npcs"
    return os.path.abspath(
        os.path.join(
            KENSHI_MOD_DIR,
            "Kayak",
            "KayakDB",
            "Campaigns",
            campaign or ACTIVE_CAMPAIGN,
            "categories",
            category_name,
        )
    )


def _npc_dialogue_path(campaign, safe_fields, fallback_name=""):
    category = str(
        safe_fields.get("_entity_category")
        or safe_fields.get("category")
        or "campaign_npcs"
    ).strip().lower()
    entity_name = str(safe_fields.get("_entity_name") or "").strip()
    if not entity_name and fallback_name:
        entity_name = re.sub(r"[^\w\-]", "_", str(fallback_name)).strip("_")
    if not entity_name:
        return ""
    return os.path.join(_npc_category_root(campaign, category), entity_name, "dialogue.txt")


def _cleanup_pending_profiles(campaign=None, reason="manual"):
    active_campaign = campaign or ACTIVE_CAMPAIGN
    result = {"deleted": 0, "kept": 0, "favorites": 0, "errors": []}

    if not (KAYAK_ENABLED and kayak):
        return result

    try:
        all_fields = kayak.get_all_npc_fields(active_campaign) or {}
    except Exception as cleanup_err:
        logging.warning(f"CLEANUP: failed to list NPCs for {active_campaign}: {cleanup_err}")
        result["errors"].append(str(cleanup_err))
        return result

    favorites = set(load_settings().get("favorites", []))
    deleted_any = False

    for display_name, field_dict in all_fields.items():
        safe_fields = field_dict or {}
        category = str(
            safe_fields.get("_entity_category")
            or safe_fields.get("category")
            or "campaign_npcs"
        ).strip().lower()
        if category != "campaign_npcs":
            continue
        profile_state = str(safe_fields.get("profile_state") or "").strip().lower()
        folder_name = str(safe_fields.get("_entity_name") or "").strip()
        if not folder_name:
            result["kept"] += 1
            continue

        npcs_root = _npc_category_root(active_campaign, category)
        if not os.path.isdir(npcs_root):
            result["kept"] += 1
            continue
        entity_dir = os.path.abspath(os.path.join(npcs_root, folder_name))
        try:
            if os.path.commonpath([npcs_root, entity_dir]) != npcs_root:
                raise ValueError(f"unsafe NPC path: {entity_dir}")
        except Exception as path_err:
            logging.warning(f"CLEANUP: refusing to touch {folder_name}: {path_err}")
            result["errors"].append(str(path_err))
            continue

        sid = str(
            safe_fields.get("persistent_id")
            or safe_fields.get("id")
            or safe_fields.get("runtime_id")
            or display_name
            or folder_name
        ).strip()
        is_favorite = sid in favorites
        has_dialogue = _string_truthy(safe_fields.get("has_dialogue"))
        dialogue_path = _npc_dialogue_path(active_campaign, safe_fields, display_name)
        if not has_dialogue and os.path.isfile(dialogue_path):
            try:
                has_dialogue = os.path.getsize(dialogue_path) > 0
            except OSError:
                has_dialogue = False

        pending_stub = is_placeholder_profile({
            "Personality": safe_fields.get("personality", ""),
            "Backstory": safe_fields.get("backstory", ""),
            "_profile_state": profile_state,
        })
        if (not pending_stub) or has_dialogue or is_favorite:
            result["kept"] += 1
            if is_favorite:
                result["favorites"] += 1
            continue

        if os.path.isdir(entity_dir):
            shutil.rmtree(entity_dir)
            deleted_any = True
            result["deleted"] += 1
            logging.info(f"CLEANUP[{reason}]: removed pending NPC '{display_name}' ({sid})")

    if deleted_any:
        character_gateway.invalidate()
        if kayak and getattr(kayak, "hub", None):
            try:
                kayak.hub._post("/campaign/reload_index", {"campaign": active_campaign})
            except Exception as reload_err:
                logging.warning(f"CLEANUP[{reason}]: failed to reload Kayak index ({reload_err})")
                result["errors"].append(str(reload_err))
        if _HAVE_CHARACTER_HANDLER:
            try:
                sync_registry_from_kayak(campaign=active_campaign, kayak_bridge=kayak)
            except Exception as sync_err:
                logging.warning(f"CLEANUP[{reason}]: registry sync failed ({sync_err})")
                result["errors"].append(str(sync_err))

    return result


_STARTUP_PENDING_CLEANUP = _cleanup_pending_profiles(reason="startup")
if _STARTUP_PENDING_CLEANUP.get("deleted"):
    logging.info(
        f"CLEANUP[startup]: removed {_STARTUP_PENDING_CLEANUP['deleted']} pending NPCs "
        f"({_STARTUP_PENDING_CLEANUP['favorites']} favorites preserved)"
    )


def get_character_data(name, context="", char_id=None, skip_generate=False):
    """
    Resolve and return an NPC's character data as an SS transport dict.

    Read path:  gateway.read() → Kayak (single source of truth)
    Write path: gateway.write() only when data was actually changed or generated.

    No legacy migration. No filesystem access. No unconditional save.
    """
    # CRITICAL: If the name contains a pipe (serial ID), split it to get the clean name.
    if '|' in name:
        name_parts = name.split('|')
        name = name_parts[0]
        if not char_id and len(name_parts) > 1:
            char_id = name_parts[1]

    # Resolve request-local context and then fall back to the live cache.
    ctx_data = _parse_context_dict(context, fallback_name=name)
    live_explicit_id = (
        ctx_data.get("runtime_id")
        or ctx_data.get("id")
        or char_id
    )
    _, live_ctx = resolve_live_context(name=name, context=ctx_data, explicit_id=live_explicit_id)
    live_ctx = live_ctx or {}

    name = str(name).strip()
    _faction = (ctx_data.get("faction") or live_ctx.get("faction") or "").strip()
    persistent_id = str(ctx_data.get("persistent_id") or live_ctx.get("persistent_id") or "").strip() or None
    if not persistent_id and _is_strong_uid(char_id):
        persistent_id = str(char_id).strip()
    runtime_id = str(
        ctx_data.get("runtime_id")
        or ctx_data.get("id")
        or live_ctx.get("runtime_id")
        or live_ctx.get("id")
        or (char_id if not persistent_id else "")
        or ""
    ).strip() or None

    if KAYAK_ENABLED and kayak and hasattr(kayak, "ensure_npc_identity") and name:
        try:
            hydrated_fields = kayak.ensure_npc_identity(
                name,
                target_npc_id=(persistent_id or runtime_id or None),
                live_ctx={
                    "persistent_id": persistent_id or "",
                    "runtime_id": runtime_id or "",
                    "id": runtime_id or "",
                },
                campaign=ACTIVE_CAMPAIGN,
            ) or {}
            hydrated_pid = str(hydrated_fields.get("persistent_id") or "").strip() or None
            hydrated_rid = str(hydrated_fields.get("runtime_id") or "").strip() or None
            if hydrated_pid and not persistent_id:
                persistent_id = hydrated_pid
            if hydrated_rid and not runtime_id:
                runtime_id = hydrated_rid
        except Exception as hydrate_err:
            logging.debug(f"IDENTITY-BIND: skipped for {name} ({hydrate_err})")

    base_sid = make_storage_id(name, _faction)

    # Disambiguate ALL NPCs when no persistent_id is available — not just generics.
    # Two named NPCs with the same name+faction (e.g. two "Ruka" in Shek Kingdom)
    # would otherwise share base_sid and silently overwrite each other's profiles.
    _safe_id = persistent_id or runtime_id or str(char_id or "").strip()
    if _is_strong_uid(persistent_id):
        storage_id = persistent_id
    elif _safe_id:
        storage_id = f"{base_sid}_{_safe_id}"
    else:
        storage_id = base_sid

    pending_key, pending_data = _pending_lookup(
        name=name,
        context=ctx_data,
        explicit_id=char_id,
        live_ctx=live_ctx,
        storage_id=storage_id,
    )

    # ── READ: Gateway is the sole read path (Kayak source of truth) ──
    # Try disambiguated key first, then fall back to legacy base_sid
    # so existing profiles saved under the old Name_Faction format are still found.
    data = character_gateway.read(name, persistent_id or storage_id, ACTIVE_CAMPAIGN)
    if not data and storage_id != base_sid:
        data = character_gateway.read(name, base_sid, ACTIVE_CAMPAIGN)
        if data:
            data["ID"] = storage_id
            logging.info(f"ID-COMPAT: {name} found under legacy key '{base_sid}', will save as '{storage_id}'")

    hydration_job = _load_hydration_job(name=name, storage_id=storage_id, campaign=ACTIVE_CAMPAIGN)
    transient_existing = pending_data or (_profile_from_hydration_job(hydration_job, ready=False) if hydration_job else None)

    if data and not profile_needs_upgrade(data) and not _profile_is_hydrating(data) and pending_key:
        _pending_discard(
            name=name,
            context=ctx_data,
            explicit_id=char_id,
            live_ctx=live_ctx,
            storage_id=storage_id,
            key=pending_key,
        )

    if data and not _profile_is_hydrating(data) and hydration_job:
        _delete_hydration_job(name=name, storage_id=storage_id, campaign=ACTIVE_CAMPAIGN)
        hydration_job = None

    if data and _profile_is_hydrating(data):
        if not hydration_job:
            hydration_job = _sync_hydration_job(
                data,
                name=name,
                storage_id=storage_id,
                campaign=ACTIVE_CAMPAIGN,
                live_ctx=copy.deepcopy(live_ctx),
            )
        if pending_data:
            hydration_job = _sync_hydration_job_from_pending(
                pending_data,
                name=name,
                storage_id=storage_id,
                campaign=ACTIVE_CAMPAIGN,
                live_ctx=copy.deepcopy(pending_data.get("_pending_live_context") or live_ctx),
            ) or hydration_job
        resumed = _resume_hydration(name, storage_id, ACTIVE_CAMPAIGN, live_ctx=live_ctx)
        data = resumed or (_profile_from_hydration_job(hydration_job, ready=False) if hydration_job else data)
        if data and not _profile_is_hydrating(data) and pending_key:
            _pending_discard(
                name=name,
                context=ctx_data,
                explicit_id=char_id,
                live_ctx=live_ctx,
                storage_id=storage_id,
                key=pending_key,
            )
    elif profile_needs_upgrade(data):
        transient_existing = pending_data or data
        if skip_generate:
            return transient_existing
        data = None

    if not data:
        # Last-chance recovery for UI/profile reads: some callers reach this path
        # with a runtime id, persistent id, or name variant that doesn't match the
        # first computed storage key. Before falling back to the transient
        # "A quiet traveler / A <race> from <faction>" stub, force fresh Kayak
        # reloads using every plausible identity we have.
        retry_ids = []
        for candidate in (
            persistent_id,
            storage_id,
            base_sid,
            runtime_id,
            ctx_data.get("runtime_id"),
            ctx_data.get("id"),
            live_ctx.get("runtime_id"),
            live_ctx.get("id"),
            char_id,
        ):
            text = str(candidate or "").strip()
            if text and text not in retry_ids:
                retry_ids.append(text)

        for retry_id in retry_ids:
            data = character_gateway.reload(name, retry_id, ACTIVE_CAMPAIGN)
            if data:
                logging.info(f"READ-RECOVERY: {name} recovered via reload id '{retry_id}'")
                break

        if not data:
            data = character_gateway.reload(name, None, ACTIVE_CAMPAIGN)
            if data:
                logging.info(f"READ-RECOVERY: {name} recovered via plain-name reload")

        if not data and hydration_job:
            if pending_data:
                hydration_job = _sync_hydration_job_from_pending(
                    pending_data,
                    name=name,
                    storage_id=storage_id,
                    campaign=ACTIVE_CAMPAIGN,
                    live_ctx=copy.deepcopy(pending_data.get("_pending_live_context") or live_ctx),
                ) or hydration_job
            resumed = _resume_hydration(name, storage_id, ACTIVE_CAMPAIGN, live_ctx=live_ctx)
            data = resumed or _profile_from_hydration_job(hydration_job, ready=False)

    # If we recovered a pending_intro placeholder during reload, do not let it
    # short-circuit first-contact generation. Keep its history, but continue on
    # to profile generation for the speaking NPC.
    if data and profile_needs_upgrade(data):
        if skip_generate:
            return pending_data or data
        transient_existing = pending_data or transient_existing or data
        logging.debug(f"TRANS-PATH-RECOVERED-PENDING: {name} (forcing real generation after reload)")
        data = None

    # Truncate bloated dialogue
    if data and "ConversationHistory" in data and len(data["ConversationHistory"]) > DIALOGUE_HISTORY_LIMIT:
        data["ConversationHistory"] = data["ConversationHistory"][-DIALOGUE_HISTORY_LIMIT:]

    # Warn if incoming serial ID doesn't match what's stored — surfaces drift
    if data and char_id and data.get("ID"):
        incoming_id = str(char_id).strip()
        stored_id = str(data["ID"]).strip()
        if incoming_id and stored_id != incoming_id and not re.fullmatch(r"-?\d+", incoming_id):
            logging.warning(f"ID mismatch for '{name}': stored={stored_id}, incoming={incoming_id}")

    # Schema safety net: fill any missing fields with defaults.
    # The gateway provides these, but belt+suspenders for legacy fallback path.
    if data:
        if "ConversationHistory" not in data: data["ConversationHistory"] = []
        if "Relation" not in data: data["Relation"] = 0
        if "Race" not in data: data["Race"] = "Unknown"
        if "Sex" not in data: data["Sex"] = "Unknown"
        if "Faction" not in data: data["Faction"] = "Unknown"
        if "OriginFaction" not in data: data["OriginFaction"] = "Unknown"
        if "Job" not in data: data["Job"] = "None"
        if "Traits" not in data:
            data["Traits"] = generate_npc_traits(
                data.get("Faction", "Unknown"),
                data.get("Race", "Unknown"),
                data.get("OriginFaction", "Unknown")
            )
        if _profile_is_hydrating(data):
            data["_profile_state"] = "hydrating_intro"
            data["_has_dialogue"] = str(data.get("_has_dialogue") or "0")
        elif profile_needs_upgrade(data):
            data["_profile_state"] = "pending_intro"
        elif not data.get("_profile_state"):
            data["_profile_state"] = "ready"
        if not _profile_is_hydrating(data):
            data["_has_dialogue"] = "1" if data.get("ConversationHistory") else str(data.get("_has_dialogue") or "0")

    # Enrich unknown metadata from live context — save ONLY if something changed
    if ctx_data and data:
        try:
            needs_save = False
            for field, ctx_key, unknowns in [
                ("Race",          "race",            ("Unknown",)),
                ("Sex",           "gender",          ("Unknown", None)),
                ("Faction",       "faction",         ("Unknown",)),
                ("OriginFaction", "origin_faction",  ("Unknown",)),
                ("Job",           "job",             ("None", "Unknown")),
            ]:
                current = ctx_data.get(ctx_key)
                if data.get(field) in unknowns and current and current not in unknowns:
                    logging.info(f"Updating {field} for {name}: {current}")
                    data[field] = current
                    needs_save = True

            if needs_save:
                data["ID"] = data.get("ID") or storage_id
                if _profile_is_hydrating(data):
                    character_gateway.write_entity_only(data, ACTIVE_CAMPAIGN, reason="metadata_fix")
                else:
                    character_gateway.write(data, ACTIVE_CAMPAIGN, reason="metadata_fix")
        except Exception as e:
            logging.error(f"Error updating character metadata from context: {e}")

    if not data:
        # No data and skip_generate → transient placeholder (never saved)
        if skip_generate:
            logging.debug(f"TRANS-PATH-1: {name} (skip_generate=True)")
            return _build_pending_profile(name, storage_id, ctx_data, live_ctx, transient_existing)

        # Generation Lock: Prevent parallel single gens for the same NPC
        with PROGRESS_LOCK:
            if storage_id in PROFILES_IN_PROGRESS:
                logging.debug(f"TRANS-PATH-2: {name} (Already in progress: {storage_id})")
                if transient_existing:
                    return transient_existing
                return _build_pending_profile(name, storage_id, ctx_data, live_ctx)
            PROFILES_IN_PROGRESS.add(storage_id)

        try:
            profile = generate_character_profile(name, context)
            if profile is None:
                logging.debug(f"TRANS-PATH-3: {name} (Generator returned None)")
                if transient_existing:
                    return transient_existing
                return _build_pending_profile(name, storage_id, ctx_data, live_ctx)
            data = {
                "ID": storage_id,
                "Name": name,
                "Race": profile.get("Race", "Unknown"),
                "Sex": profile.get("Sex", "Unknown"),
                "Faction": profile.get("Faction", "Unknown"),
                "OriginFaction": profile.get("OriginFaction", "Unknown"),
                "Job": profile.get("Job", "None"),
                "Personality": profile.get("Personality", "Unknown"),
                "Backstory": profile.get("Backstory", "Unknown"),
                "SpeechQuirks": profile.get("SpeechQuirks", ""),
                "ConversationHistory": [],
                "Relation": 0,
                "Traits": generate_npc_traits(
                    profile.get("Faction", "Unknown"),
                    profile.get("Race", "Unknown"),
                    profile.get("OriginFaction", "Unknown"),
                ),
            }
            if transient_existing:
                data["ConversationHistory"] = list(transient_existing.get("ConversationHistory", []))
                data["Relation"] = transient_existing.get("Relation", 0)

            _mark_profile_ready(data)

            # Gate: Only block feral NPCs from Kayak persistence.
            # Animals need real entities, but they must keep animal-specific prompts.
            _race = data.get("Race") or (ctx_data.get("race", "Unknown") if ctx_data else "Unknown")
            _fact = data.get("Faction") or (ctx_data.get("faction", "Unknown") if ctx_data else "Unknown")
            _persona_cat = get_persona_category(_race, _fact, name=name, source="first_contact_save")
            data["_persona_category"] = _persona_cat
            _should_save = _persona_cat != "feral"

            if _should_save:
                _staged_job = _sync_hydration_job(
                    data,
                    name=name,
                    storage_id=storage_id,
                    campaign=ACTIVE_CAMPAIGN,
                    live_ctx=copy.deepcopy(live_ctx or ctx_data),
                )
                if _staged_job:
                    data = _profile_from_hydration_job(_staged_job, ready=False)
                    logging.info(
                        f"HYDRATE: staged generated profile for {name} "
                        f"(history={len(_staged_job.get('conversation_history', []) or [])})"
                    )
                    if data.get("ConversationHistory"):
                        hydrated, hydrate_ok = _run_hydration_job(
                            _staged_job,
                            campaign=ACTIVE_CAMPAIGN,
                            live_ctx=copy.deepcopy(live_ctx or ctx_data),
                        )
                        if hydrate_ok and hydrated:
                            data = hydrated
                            _pending_discard(
                                name=name,
                                context=ctx_data,
                                explicit_id=char_id,
                                live_ctx=live_ctx,
                                storage_id=storage_id,
                                profile=transient_existing,
                                key=(transient_existing or {}).get("_pending_key") or pending_key,
                            )
                else:
                    logging.warning(f"HYDRATE: failed to stage durable job for {name}; falling back to direct write")
                    character_gateway.write(data, ACTIVE_CAMPAIGN, reason="generation")

                    if KAYAK_ENABLED and _HAVE_CHARACTER_HANDLER:
                        try:
                            _gen_pid = create_from_profile(
                                data,
                                npc_name=name,
                                campaign=ACTIVE_CAMPAIGN,
                                kayak_bridge=kayak
                            )
                            if _gen_pid:
                                data["persistent_id"] = _gen_pid
                                logging.info(f"CHARACTER: {name} written to Kayak (id={_gen_pid})")
                        except Exception as _kayak_gen_e:
                            logging.warning(f"CHARACTER: Kayak write failed for {name} ({_kayak_gen_e})")
                    _pending_discard(
                        name=name,
                        context=ctx_data,
                        explicit_id=char_id,
                        live_ctx=live_ctx,
                        storage_id=storage_id,
                        profile=transient_existing,
                        key=(transient_existing or {}).get("_pending_key") or pending_key,
                    )
            else:
                logging.info(f"GENERATION: Skipped saving generated profile for {name} ({storage_id}) [feral blocked]")

        finally:
            with PROGRESS_LOCK:
                if storage_id in PROFILES_IN_PROGRESS:
                    PROFILES_IN_PROGRESS.remove(storage_id)

    # Enrich with world-index data
    if name in WORLD_INDEX:
        data = {**data}
        if "ConversationHistory" in data:
            data["ConversationHistory"] = list(data["ConversationHistory"])
        data["SourcePlatoons"] = WORLD_INDEX[name]

    # No unconditional save here. Writes happen only when:
    #   - reason="generation" (new profile created above)
    #   - reason="metadata_fix" (unknown fields enriched above)
    # All other saves (dialogue, rename, regen) are handled by their own routes.
    return data

# Character I/O is handled by character_gateway (ss_character_gateway.py).
# Utility functions (make_storage_id, should_save_profile, etc.) live in ss_persistence.


@app.route('/log', methods=['POST'])
def log_dialogue():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
        
    npc_name = data.get('npc', 'Someone')
    player_name = get_effective_player_name(data.get('player', 'Drifter'))
    player_message = data.get('message', '')
    npc_response = data.get('response', '')
    context = data.get('context', '')
    npc_id = extract_id_from_context(context)

    char_data = get_character_data(npc_name, context, char_id=npc_id)
    
    # CRITICAL FIX: Use the stable ID from char_data, NOT the volatile serial ID
    storage_id = char_data.get("ID") or npc_name
    
    time_prefix = get_current_time_prefix()
    
    if player_message:
        char_data["ConversationHistory"].append(f"{time_prefix}{player_name}: {player_message}")
        record_event_to_history("DIALOGUE", player_name, npc_name, player_message)

    if npc_response:
        char_data["ConversationHistory"].append(f"{time_prefix}{npc_name}: {npc_response}")
        record_event_to_history("DIALOGUE", npc_name, player_name, npc_response)
        
    if len(char_data["ConversationHistory"]) > DIALOGUE_HISTORY_LIMIT:
        char_data["ConversationHistory"] = char_data["ConversationHistory"][-DIALOGUE_HISTORY_LIMIT:]

    _, _log_live_ctx = resolve_live_context(
        name=npc_name,
        context=char_data,
        explicit_id=char_data.get("ID") or storage_id,
    )
    _log_live_ctx = _log_live_ctx or {}
    _hydration_job = _load_hydration_job(name=npc_name, storage_id=storage_id, campaign=ACTIVE_CAMPAIGN)
    if _profile_is_hydrating(char_data) or _hydration_job:
        _synced_job = _sync_hydration_job(
            char_data,
            name=npc_name,
            storage_id=storage_id,
            campaign=ACTIVE_CAMPAIGN,
            live_ctx=_log_live_ctx,
        )
        hydrated, hydrate_ok = _run_hydration_job(
            _synced_job,
            campaign=ACTIVE_CAMPAIGN,
            live_ctx=_log_live_ctx,
        ) if _synced_job else (None, False)
        if hydrate_ok and hydrated:
            char_data = hydrated
            _pending_discard(
                name=npc_name,
                context=context,
                explicit_id=npc_id,
                live_ctx=_log_live_ctx,
                storage_id=storage_id,
                profile=char_data,
            )
        else:
            logging.info(f"LOG: staged hydrating profile for {npc_name}; job will resume on next read")
    elif should_save_profile(npc_name, storage_id, char_data):
        # Only dialogue changed — write dialogue without rewriting entity.txt.
        # This is the key fix that prevents biography overwrites on every chat.
        character_gateway.write_dialogue_only(
            npc_name, storage_id,
            char_data["ConversationHistory"],
            campaign=ACTIVE_CAMPAIGN,
        )
    logging.info(f"LOG [{npc_name} ({storage_id})]: {npc_response}")
    return jsonify({"status": "ok"})

@app.route('/get_unique_identity', methods=['POST'])
def get_unique_identity():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    current_name = str(data.get('name', 'Someone')).strip()
    race = data.get('race', 'Human')
    gender = data.get('gender', 'Neutral')
    
    if is_npc_name_generic(current_name):
        new_name = generate_unique_lore_name(gender=gender, title_prefix=current_name)
        logging.info(f"IDENTITY: Assigning unique {gender} name '{new_name}' to generic NPC '{current_name}'")
        return jsonify({
            "status": "rename",
            "new_name": new_name
        })
    
    return jsonify({"status": "ok", "name": current_name})

@app.route('/get_batch_identities', methods=['POST'])
def get_batch_identities():
    batch = request.json # Expect list of {serial, name, gender, race, persistent_id}
    if not batch or not isinstance(batch, list):
        return jsonify({"status": "error", "message": "Invalid batch format"}), 400
    settings = load_settings()
    enable_renamer = settings.get("enable_renamer", True)
    enable_animal_renamer = settings.get("enable_animal_renamer", True)

    # Global kill-switch for renamer.
    if not enable_renamer:
        return jsonify([
            {"serial": item.get("serial"), "status": "ok"}
            for item in batch
        ])
    
    preserve = RENAMING_RULES.get("preserve_existing_named", True)
    results = []
    used_names = set(get_used_names())
    rename_count = 0
    # Protect BOTH the stock speaker and the selected one — with SelectedSpeaker
    # active they are different characters and neither may be auto-renamed.
    _protected = (PLAYER_CONTEXT or {}), get_effective_player_context()
    player_names = {
        n for n in (str(c.get("name") or "").strip().lower() for c in _protected) if n
    }
    player_pids = {
        p for p in (str(c.get("persistent_id") or "").strip() for c in _protected) if p
    }
    for item in batch:
        serial = item.get('serial')
        persistent_id = item.get('persistent_id', '')
        current_name = str(item.get('name', 'Someone')).strip()
        gender = item.get('gender', 'Neutral')
        race = item.get('race', 'Unknown')
        lower_name = current_name.lower()

        # Never rename the player.
        if (lower_name in player_names) or (
            persistent_id and str(persistent_id).strip() in player_pids
        ):
            results.append({"serial": serial, "status": "ok"})
            continue

        # If preserve_existing_named is on, check if this NPC already has a
        # different name in Kayak (was renamed before). If so, skip.
        # CIRCUIT BREAKER: Skip Kayak read if breaker is tripped to avoid lag.
        _kayak_avail = character_gateway.available
        if kayak_hub and getattr(kayak_hub, "_circuit_broken", False):
            _kayak_avail = False

        if preserve and persistent_id and _kayak_avail:
            existing = character_gateway.read(current_name, str(persistent_id).strip(), ACTIVE_CAMPAIGN)
            if existing:
                orig = str(existing.get("_original_name") or existing.get("Name") or "").strip()
                curr = str(existing.get("Name") or "").strip()
                if orig and curr and orig != curr:
                    # Already been renamed previously — do not rename again.
                    results.append({"serial": serial, "status": "ok"})
                    continue
        
        is_generic = is_npc_name_generic(current_name)
        
        if is_generic:
            is_humanoid = False
            for hr in ["greenlander", "scorchlander", "shek", "skeleton", "hive", "human"]:
                if hr in race.lower():
                    is_humanoid = True
                    break

            if not enable_animal_renamer and not is_humanoid:
                logging.info(f"IDENTITY-BATCH: Skipping animal/machine rename for '{current_name}' (race: {race}) due to settings.")
                results.append({"serial": serial, "status": "ok"})
                continue

            new_name = generate_unique_lore_name(gender=gender, used_names=used_names, title_prefix=current_name)
            used_names.add(new_name.lower())
            results.append({
                "serial": serial,
                "status": "rename",
                "new_name": new_name
            })
            logging.info(f"IDENTITY-BATCH: '{current_name}' → '{new_name}' (serial {serial})")
            rename_count += 1
        else:
            results.append({
                "serial": serial,
                "status": "ok"
            })
            
    if results:
        logging.info(f"IDENTITY: Batch processed {len(results)} items. Renamed: {rename_count}")
    return jsonify(results)


@app.route('/rename', methods=['POST'])
def rename_character():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    context = data.get('context', '')
    
    if not old_name or not new_name:
        return jsonify({"status": "error", "message": "Missing names"}), 400
        
    logging.info(f"RENAME: Attempting to rename '{old_name}' to '{new_name}'")
    
    # 1. Resolve existing profile/state
    char_data = get_character_data(old_name, context, skip_generate=True)
    transient_ctx = _parse_context_dict(context, fallback_name=old_name)
    _, live_transient_ctx = resolve_live_context(
        name=old_name,
        context=transient_ctx,
        explicit_id=transient_ctx.get("runtime_id") or transient_ctx.get("id"),
    )
    live_transient_ctx = live_transient_ctx or {}
    existing_hydration_job = _load_hydration_job(
        name=old_name,
        storage_id=str((char_data or {}).get("ID") or "").strip(),
        campaign=ACTIVE_CAMPAIGN,
    )
    persisted_existing = character_gateway.read(
        old_name,
        str((char_data or {}).get("ID") or "").strip() or None,
        ACTIVE_CAMPAIGN,
    ) if char_data else None
    is_hydrating = _profile_is_hydrating(char_data) or bool(existing_hydration_job)
    needs_intro = profile_needs_upgrade(char_data)

    if is_hydrating:
        logging.info(f"RENAME: '{old_name}' is mid-hydration; preserving generated profile.")
        char_data = copy.deepcopy(char_data or {})
    elif needs_intro:
        logging.info(f"RENAME: '{old_name}' is pending first contact; saving identity only.")
        char_data = _build_pending_profile(
            old_name,
            char_data.get("ID")
            or make_storage_id(old_name, transient_ctx.get("faction") or live_transient_ctx.get("faction", ""), context=transient_ctx),
            transient_ctx,
            live_transient_ctx,
            existing=char_data,
        )
    else:
        _mark_profile_ready(char_data)

    old_id = char_data.get("ID")
    if not old_id:
        return jsonify({"status": "error", "message": "Profile ID resolution failed"}), 500

    # 2. Compute new storage ID
    if _is_strong_uid(old_id):
        new_id = old_id  # Persistent ID is stable — reuse it
    else:
        new_id = make_storage_id(new_name, char_data.get("Faction", ""), context=transient_ctx)

    rename_runtime_hint = str(
        transient_ctx.get("runtime_id")
        or transient_ctx.get("id")
        or live_transient_ctx.get("runtime_id")
        or live_transient_ctx.get("id")
        or ""
    ).strip() or None

    existing_original = str(
        (char_data or {}).get("_original_name")
        or (persisted_existing or {}).get("_original_name")
        or ""
    ).strip()
    hinted_original = (
        _lookup_original_name_hint(old_name, rename_runtime_hint or old_id, ACTIVE_CAMPAIGN)
        or _lookup_original_name_hint(new_name, rename_runtime_hint or new_id, ACTIVE_CAMPAIGN)
    )
    resolved_original = existing_original if existing_original != new_name else ""
    if hinted_original and (not resolved_original or resolved_original in {old_name, new_name}):
        resolved_original = hinted_original
    if not resolved_original:
        resolved_original = str(old_name).strip()
    if resolved_original:
        char_data["_original_name"] = resolved_original
        _store_original_name_hint(
            resolved_original,
            names=[old_name, new_name],
            entity_ids=[old_id, new_id, rename_runtime_hint],
            campaign=ACTIVE_CAMPAIGN,
        )

    # 3. Update internal data
    char_data["Name"] = new_name
    char_data["ID"] = new_id
    if is_hydrating:
        _mark_profile_hydrating(char_data)
    elif needs_intro:
        char_data["_profile_state"] = "pending_intro"
        char_data["_has_dialogue"] = "1" if char_data.get("ConversationHistory") else "0"
    else:
        _mark_profile_ready(char_data)

    if needs_intro:
        runtime_hint = rename_runtime_hint
        pending_key = char_data.get("_pending_key")
        _pending_store(
            char_data,
            name=new_name,
            context=transient_ctx,
            explicit_id=runtime_hint or new_id,
            live_ctx=live_transient_ctx,
            storage_id=new_id,
            replace_keys=[pending_key, old_id, old_name],
        )
        rename_live_ctx = dict(live_transient_ctx or transient_ctx or {})
        rename_live_ctx["name"] = new_name
        if runtime_hint:
            rename_live_ctx["runtime_id"] = runtime_hint
            rename_live_ctx["id"] = runtime_hint
        if new_id:
            rename_live_ctx["storage_id"] = new_id
        store_live_context(rename_live_ctx, name=new_name, explicit_id=(runtime_hint or new_id))
        character_gateway.invalidate(old_name, old_id)
        character_gateway.invalidate(new_name, new_id)
    elif is_hydrating:
        runtime_hint = rename_runtime_hint
        rename_live_ctx = dict(live_transient_ctx or transient_ctx or {})
        rename_live_ctx["name"] = new_name
        if runtime_hint:
            rename_live_ctx["runtime_id"] = runtime_hint
            rename_live_ctx["id"] = runtime_hint
        if new_id:
            rename_live_ctx["storage_id"] = new_id

        _rename_hydration_job(
            old_name,
            old_id,
            new_name,
            new_id,
            campaign=ACTIVE_CAMPAIGN,
            profile=char_data,
            live_ctx=rename_live_ctx,
        )
        store_live_context(rename_live_ctx, name=new_name, explicit_id=(runtime_hint or new_id))

        if persisted_existing:
            try:
                character_gateway.write_entity_only(char_data, ACTIVE_CAMPAIGN, reason="rename")
            except Exception as e:
                logging.error(f"RENAME: hydrating entity write failed: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500

            if old_id != new_id:
                character_gateway.invalidate(old_name, old_id)

            if KAYAK_ENABLED and kayak and hasattr(kayak, "on_rename"):
                try:
                    kayak.on_rename(old_name, new_name, str(new_id), campaign=ACTIVE_CAMPAIGN)
                except Exception as _rename_err:
                    logging.warning(f"RENAME: Kayak rename hook failed ({_rename_err})")

        character_gateway.invalidate(old_name, old_id)
        character_gateway.invalidate(new_name, new_id)
    else:
        # 4. Write updated profile via gateway
        try:
            character_gateway.write(char_data, ACTIVE_CAMPAIGN, reason="rename")
        except Exception as e:
            logging.error(f"RENAME: gateway write failed: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

        # 5. Invalidate old cache entry if ID changed
        if old_id != new_id:
            character_gateway.invalidate(old_name, old_id)

        # 6. Notify Kayak bridge to rename the entity folder in KayakDB
        if KAYAK_ENABLED and kayak and hasattr(kayak, "on_rename"):
            try:
                kayak.on_rename(old_name, new_name, str(new_id), campaign=ACTIVE_CAMPAIGN)
            except Exception as _rename_err:
                logging.warning(f"RENAME: Kayak rename hook failed ({_rename_err})")

    logging.info(f"RENAME: {old_name} → {new_name} (ID: {old_id} → {new_id})")
    return jsonify({"status": "ok", "new_id": new_id})

@app.route('/ambient', methods=['POST'])
def ambient_event():
    debug_logger.debug("ROUTE: /ambient [POST]")
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    
    npcs_data = data.get('npcs', [])
    player_name = get_effective_player_name(data.get('player', 'Drifter'))
    player_name_clean = _clean_npc_name(player_name)
    
    logging.info(f"RADIANT: Received ambient banter request ({len(npcs_data)} NPCs nearby)")
    
    if not npcs_data:
        return jsonify({"status": "ignore"})
    if direct_chat_active():
        logging.info("RADIANT: Deferred/ignored because a direct chat is active.")
        return jsonify({"status": "ignore"})

    # Build profiles for nearby characters
    char_profiles = ""
    name_to_id = {}
    speaker_cards = []

    # Filter out recently-used speakers so one NPC doesn't dominate every bark.
    candidate_npcs = []
    for npc in npcs_data:
        name = npc.get('name', 'Unknown') if isinstance(npc, dict) else str(npc)
        explicit_id = None
        context = None
        if isinstance(npc, dict):
            explicit_id = npc.get("storage_id") or npc.get("id")
            context = npc
            if context_is_dead(_parse_context_dict(npc, fallback_name=name)):
                logging.info(f"RADIANT FILTER: skipped dead nearby NPC '{name}'")
                continue
        if not ambient_candidate_allowed(name=name, context=context, explicit_id=explicit_id):
            continue
        candidate_npcs.append(npc)

    _ambient_settings = load_settings()
    _ambient_speaker_limit = max(2, int(_ambient_settings.get("ambient_speaker_limit", 12)))
    npc_limit = candidate_npcs[:_ambient_speaker_limit] # Small pool keeps ambient light and more varied
    if len(npc_limit) < 2:
        logging.info("RADIANT: Skipping ambient - insufficient eligible speakers after cooldown filters.")
        return jsonify({"status": "ignore"})

    # 1. Pre-check for missing profiles to batch generate
    missing_npcs = []
    queued_targets = set()
    for npc in npcs_data:
        if isinstance(npc, dict):
            name = _clean_npc_name(npc.get('name', 'Unknown'))
            if not name or name == player_name_clean:
                continue
            if context_is_dead(_parse_context_dict(npc, fallback_name=name)):
                continue
            if name.lower() in CANON_CHARACTERS or "your squad" in name.lower():
                continue
            npc_ctx = _parse_context_dict(npc, fallback_name=name)
            queue_key = str(
                npc_ctx.get("persistent_id")
                or npc_ctx.get("storage_id")
                or npc_ctx.get("runtime_id")
                or npc_ctx.get("id")
                or name
            ).strip()
            if queue_key in queued_targets:
                continue

            # Queue missing profiles for every target that may receive this
            # ambient exchange so pending listeners don't lose the bark when the
            # first-contact batch write lands a few seconds later.
            info = get_character_data(
                name,
                context=json.dumps(npc_ctx),
                char_id=(
                    npc_ctx.get("persistent_id")
                    or npc_ctx.get("storage_id")
                    or npc_ctx.get("runtime_id")
                    or npc_ctx.get("id")
                ),
                skip_generate=True,
            )
            if info.get("_transient"):
                queued_targets.add(queue_key)
                missing_npcs.append(npc)
                
    if missing_npcs:
        queue_batch_profile_generation(missing_npcs)

    # 2. Extract and format profile summary for banter call (parallelised)
    def _fetch_npc_profile(npc):
        if isinstance(npc, dict):
            name = npc.get('name', 'Unknown')
            nid = npc.get('id', 0)
            d = get_character_data(name, context=json.dumps(npc), skip_generate=True)
            health = npc.get('health', 'Healthy')
            gear = npc.get('equipment', 'nothing notable')
            race = str(npc.get('race') or d.get('Race') or 'Unknown')
            faction = str(npc.get('faction') or d.get('Faction') or 'Unknown')
            persona_category = get_persona_category(race, faction, name=name, source="radiant_prompt")
            profile_line = f"\n- {name}|{nid} ({npc.get('gender')} {race}, {faction}) | Health: {health} | Gear: {gear} | Personality: {d.get('Personality', 'A traveler.')} | Category: {persona_category}"
            speaker_card = {
                "name": name,
                "id": nid,
                "gender": npc.get("gender", "Unknown"),
                "race": race,
                "faction": faction,
                "health": health,
                "gear": gear,
                "personality": d.get("Personality", "A traveler."),
                "persona_category": persona_category,
            }
        else:
            name = npc
            nid = 0
            d = get_character_data(npc, "", skip_generate=True)
            race = str(d.get('Race') or 'Unknown')
            faction = str(d.get('Faction') or 'Unknown')
            persona_category = get_persona_category(race, faction, name=name, source="radiant_prompt")
            profile_line = f"\n- {npc}|0 (Unknown {race}, {faction}) | Health: Healthy | Gear: nothing notable | Personality: {d.get('Personality', 'A traveler.')} | Category: {persona_category}"
            speaker_card = {
                "name": name,
                "id": nid,
                "gender": "Unknown",
                "race": race,
                "faction": faction,
                "health": "Healthy",
                "gear": "nothing notable",
                "personality": d.get("Personality", "A traveler."),
                "persona_category": persona_category,
            }
        return name, nid, d, profile_line, speaker_card

    _ambient_recent_per_npc = max(1, int(_ambient_settings.get("ambient_recent_dialogue_per_npc", 15)))
    recent_dialogue = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        # submit preserves order via index — collect results in original NPC order
        futures = [pool.submit(_fetch_npc_profile, npc) for npc in npc_limit]
        for future in futures:
            name, nid, d, profile_line, speaker_card = future.result()
            name_to_id[name] = nid
            if d.get("ConversationHistory"):
                recent_dialogue.extend(d["ConversationHistory"][-_ambient_recent_per_npc:])
            char_profiles += profile_line
            speaker_cards.append(speaker_card)

    # Normalize a ConversationHistory line to "Speaker: message" format,
    # stripping timestamps, (Overheard) prefix, and [ACTION: TAG] tags.
    def _normalize(line):
        k = re.sub(r'^\[Day[^\]]+\]\s*(?:\(Overheard\)\s*)?', '', line)
        k = re.sub(r'\s*\[ACTION:[^\]]*\]', '', k)
        return k.strip()

    # 1. Pull from individual NPC memories, normalized to "Speaker: message"
    all_history = [_normalize(line) for line in recent_dialogue]

    # 2. Extract global banter/chat history from EVENT_HISTORY for the current location
    location = ""
    if PLAYER_CONTEXT:
        env = PLAYER_CONTEXT.get("environment", {})
        location = env.get("town_name", "") if isinstance(env, dict) else ""

    banter_events = []
    for evt in reversed(EVENT_HISTORY):
        if (" [BANTER] " in evt or " [CHAT] " in evt):
            if not location or f"@ {location}" in evt or "@" not in evt:
                if ": " in evt:
                    msg_part = evt.split(": ", 1)[1]
                    match = re.search(r'\]\s*(.*?)\s*(?:\(.*?\))?\s*->', evt)
                    speaker = match.group(1).strip() if match else None
                    banter_events.append(f"{speaker}: {msg_part}" if speaker else msg_part)
        if len(all_history) + len(banter_events) > 100:
            break
    all_history.extend(reversed(banter_events))

    # Deduplicate, keeping most recent on collision, then take last 80
    seen_history = set()
    unique_history = []
    for line in reversed(all_history):
        if line not in seen_history:
            unique_history.append(line)
            seen_history.add(line)
    _ambient_local_history_limit = max(1, int(_ambient_settings.get("ambient_local_history_limit", 80)))
    unique_history = list(reversed(unique_history))[-_ambient_local_history_limit:]
    
    history_text = "\n".join(unique_history)
    history_block = ""
    if history_text:
        history_block = "\nRECENT LOCAL DIALOGUE (DO NOT REPEAT TOPICS OR JOKES FROM HERE):\n" + history_text

    ambient_npc_data = None
    if npc_limit and isinstance(npc_limit[0], dict):
        ambient_npc_data = get_character_data(npc_limit[0].get('name', 'Unknown'), "", skip_generate=True)

    world_lore = fetch_dynamic_lore(ambient_npc_data)
    events_str = build_events_block()

    ambient_system_prompt = ""
    if KAYAK_ENABLED and kayak:
        try:
            _env = PLAYER_CONTEXT.get("environment", {}) if isinstance(PLAYER_CONTEXT, dict) else {}
            ambient_system_prompt = kayak.build_radiant_prompt(
                speakers=speaker_cards,
                player_name=player_name,
                world_lore=world_lore,
                events=events_str,
                recent_dialogue=history_text,
                campaign=ACTIVE_CAMPAIGN,
                world_synthesis_path=os.path.join(KENSHI_SERVER_DIR, "logs", "world_synthesis.log"),
                server_town=str(_env.get("town_name") or _env.get("town") or "") if isinstance(_env, dict) else "",
                server_region=str(_env.get("biome") or _env.get("region") or "") if isinstance(_env, dict) else "",
            )
            if ambient_system_prompt:
                logging.info(f"KAYAK: Radiant prompt built for {len(speaker_cards)} nearby speakers")
        except Exception as _kayak_radiant_err:
            logging.warning(f"KAYAK: Radiant prompt build failed ({_kayak_radiant_err}) - falling back.")

    if not ambient_system_prompt:
        logging.error("KAYAK: Radiant prompt unavailable; native radiant fallback disabled for token prompt testing.")
        return jsonify({"status": "error", "reason": "kayak_radiant_prompt_unavailable"}), 500
    
    messages = [
        {"role": "system", "content": ambient_system_prompt},
        {"role": "user", "content": "Generate."}
    ]

    _radiant_source = str(data.get("source") or data.get("mode") or "").lower()
    if data.get("timed") or data.get("automatic") or "timer" in _radiant_source or "auto" in _radiant_source:
        _radiant_log_type = "radiant_timed"
    else:
        _radiant_log_type = "radiant_triggered"
    log_prompt_snapshot(_radiant_log_type, messages=messages, metadata={"campaign": ACTIVE_CAMPAIGN, "npc_count": len(npc_limit) if npc_limit else 0, "source": _radiant_source or "unknown"})
    content = call_llm(messages, max_tokens=int(load_settings().get("ambient_max_tokens", 400)), temperature=float(load_settings().get("narrative_temperature", 0.8)))  # added by Pineaxe v07 - configurable via settings.json
    if content:
        # Strip any stray [ACTION] tags that the LLM might hallucinated despite instructions
        content = re.sub(r'\[\s*[A-Z_]+(?::\s*[^\]]+)?\s*\]', '', content).strip()
        
        # Basic cleaning - remove quotes
        content = content.replace('"', '').strip()
        
        # Post-process to ensure IDs are present and speakers are real nearby NPCs.
        lines = []
        ambient_history_lines = []
        for line in content.split('\n'):
            line = line.strip()
            if not line: continue
            
            if ':' in line:
                header, msg = line.split(':', 1)
                name_part = _clean_npc_name(header.split('|')[0].strip())
                msg = msg.strip()
                
                # Hallucination check
                if not name_part or name_part.lower() == player_name.lower():
                    continue
                if name_part not in name_to_id:
                    logging.info(f"RADIANT FILTER: discarded ambient line from unknown speaker '{name_part}'")
                    continue

                # Ensure ID is present even if LLM forgot
                if '|' not in header:
                    header = f"{name_part}|{name_to_id[name_part]}"

                if not msg:
                    continue

                lines.append(f"{header.strip()}: {msg}")
                ambient_history_lines.append(f"{name_part}: {msg}")
            elif '|' in line and len(line) < 100: # Maybe just a name header LLM hallucinated
                continue
            else:
                logging.info(f"RADIANT FILTER: discarded malformed ambient line '{line[:120]}'")
                continue

        if not lines:
            logging.info("RADIANT: ambient response had no valid nearby-speaker lines after filtering.")
            return jsonify({"status": "none"})
        
        final_text = "\n".join(lines)
        
        # 5. Optimized History Update (One save per NPC)
        # Pre-load character memories for the selected speaker pool, then persist
        # the same exchange to all nearby listeners with overheard tagging.
        memories = {}
        speaker_names = set()
        nearby_lookup = {}
        nearby_names = []

        for npc_obj in npcs_data:
            raw_name = npc_obj.get('name') if isinstance(npc_obj, dict) else npc_obj
            clean_name = _clean_npc_name(raw_name)
            if not clean_name or clean_name == player_name_clean:
                continue
            if isinstance(npc_obj, dict) and context_is_dead(_parse_context_dict(npc_obj, fallback_name=clean_name)):
                continue
            if clean_name not in nearby_lookup:
                nearby_lookup[clean_name] = npc_obj
                nearby_names.append(clean_name)

        if player_name_clean and player_name_clean not in nearby_lookup:
            player_ctx = get_effective_player_context()
            player_ctx.setdefault("name", player_name)
            nearby_lookup[player_name_clean] = player_ctx
            nearby_names.append(player_name_clean)

        for npc_obj in npc_limit:
            name = _clean_npc_name(npc_obj.get('name') if isinstance(npc_obj, dict) else npc_obj)
            if not name:
                continue
            # Use skip_generate=True here just in case, though they should be generated by now
            memories[name] = get_character_data(name, context=json.dumps(npc_obj) if isinstance(npc_obj, dict) else "", skip_generate=True)

        for line in lines:
            if ':' in line:
                header, msg = line.split(':', 1)
                speaker_name = header.split('|')[0].strip()
                speaker_names.add(speaker_name)

                # Also log to global history for narrative synthesis
                speaker_faction = memories.get(speaker_name, {}).get("Faction", "None")
                record_event_to_history("BANTER", speaker_name, "Nearby", msg.strip(), actor_faction=speaker_faction)

        speaker_name_set = {_clean_npc_name(name) for name in speaker_names if _clean_npc_name(name)}
        time_prefix = get_current_time_prefix()
        for target_name in nearby_names:
            if target_name not in memories:
                target_obj = nearby_lookup.get(target_name)
                target_ctx = _parse_context_dict(target_obj, fallback_name=target_name) if isinstance(target_obj, dict) else {}
                target_context = json.dumps(target_ctx) if target_ctx else ""
                target_id = (
                    target_ctx.get("persistent_id")
                    or target_ctx.get("storage_id")
                    or target_ctx.get("runtime_id")
                    or target_ctx.get("id")
                ) if target_ctx else None
                memories[target_name] = get_character_data(
                    target_name,
                    context=target_context,
                    char_id=target_id,
                    skip_generate=True,
                )

            if "ConversationHistory" not in memories[target_name]:
                memories[target_name]["ConversationHistory"] = []

            overheard_tag = "" if target_name in speaker_name_set else "(Overheard) "
            for history_line in ambient_history_lines:
                memories[target_name]["ConversationHistory"].append(f"{time_prefix}{overheard_tag}{history_line}")

            _persist_conversation_history_target(
                target_name,
                memories,
                persist_source="ambient_persist",
            )

        mark_ambient_speakers(
            (speaker_name, name_to_id.get(speaker_name), memories.get(speaker_name))
            for speaker_name in speaker_names
        )

        logging.info(f"AMBIENT BARK:\n{final_text}")
        return jsonify({"status": "ok", "text": final_text})
    
    return jsonify({"status": "none"})

@app.route('/ping', methods=['GET', 'POST'])
def ping():
    return jsonify({"status": "ok"})

@app.route('/test_llm', methods=['GET', 'POST'])
def test_llm():
    """Verify both server and LLM connectivity."""
    try:
        messages = [{"role": "user", "content": "Keep your response extremely short. Reply with the word: Success"}]
        log_prompt_snapshot("connection_test", messages=messages, metadata={"campaign": ACTIVE_CAMPAIGN})
        response = call_llm(messages, max_tokens=10, temperature=0.7)
        if response:
            logging.info(f"TEST_LLM: Success! Response: {response}")
            # Ensure fixed key order and no extra spaces for C++ parsing
            return '{"status":"ok","llm":"ok","response":"' + response.replace('"', "'") + '"}'
        else:
            logging.error("TEST_LLM: call_llm returned None.")
            return '{"status":"ok","llm":"error","message":"Global LLM call failed."}'
    except Exception as e:
        logging.error(f"TEST_LLM: Exception during test: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/lore_debug', methods=['GET', 'POST'])
def lore_debug():
    """Debug endpoint: returns the lore sections that would be injected for a given NPC.
    Accepts optional JSON body: { "faction": "...", "race": "...", "religion": "...",
                                   "platoons": [...], "town": "...", "biome": "..." }
    Can also be called with no body (GET) to see Global-only lore.
    Example: curl http://127.0.0.1:5000/lore_debug -H "Content-Type: application/json"
             -d '{"faction":"Shek Kingdom","race":"Shek","religion":"Devoted to Kral"}'
    """
    data = request.get_json(silent=True) or {}
    # Build a synthetic npc_data from request params
    npc_data = {
        "Faction":       data.get("faction", ""),
        "Race":          data.get("race", ""),
        "SourcePlatoons": data.get("platoons", []),
        "Traits":        {"Religion": data.get("religion", "")},
    }
    # Optionally inject location context without overwriting global PLAYER_CONTEXT
    town  = data.get("town", "")
    biome = data.get("biome", "")

    env_override = {"town_name": town, "biome": biome} if (town or biome) else None

    lore_output = fetch_dynamic_lore(npc_data, env_override=env_override)

    # Build a diagnostic summary alongside the output
    search_terms = ["Global"]
    faction = npc_data.get("Faction", "")
    if faction and faction != "Unknown":
        search_terms.append(faction)
    search_terms.extend(p for p in npc_data.get("SourcePlatoons", []) if p)
    race = npc_data.get("Race", "")
    if race and race != "Unknown":
        search_terms.append(race)
    religion = npc_data.get("Traits", {}).get("Religion", "")
    if religion and religion not in ("N/A", "Unknown", "Hive-Bound"):
        search_terms.append(religion)
    if town:
        search_terms.append(town)
    if biome:
        search_terms.append(biome)

    matched_ids = [
        chunk["id"] for chunk in LORE_DATABASE
        if any(t in chunk.get("tags", []) for t in search_terms)
    ]

    return jsonify({
        "search_terms": search_terms,
        "matched_chunks": matched_ids,
        "chunk_count": len(matched_ids),
        "total_chars": len(lore_output),
        "lore_output": lore_output,
    })

@app.route('/record_major_event', methods=['POST'])
def record_major_event():
    """Append a major historical event to campaign_chronicle.json.
    Required body fields: summary (str), factions_full (list), radius (str), location (str).
    Optional: location_region (str), summary_vague (str), tags (list), day (int).
    """
    logging.info("ROUTE: /record_major_event [POST]")
    data = request.json or {}

    summary       = data.get("summary", "").strip()
    factions_full = data.get("factions_full", [])
    radius        = data.get("radius", "")
    location      = data.get("location", "").strip()

    if not summary:
        return jsonify({"status": "error", "message": "Missing required field: summary"}), 400
    if not isinstance(factions_full, list):
        return jsonify({"status": "error", "message": "factions_full must be a list"}), 400
    if radius not in ("local", "regional", "global"):
        return jsonify({"status": "error", "message": "radius must be local|regional|global"}), 400
    if not location:
        return jsonify({"status": "error", "message": "Missing required field: location"}), 400

    event = {
        "summary":         summary,
        "factions_full":   factions_full,
        "radius":          radius,
        "location":        location,
        "location_region": data.get("location_region", "").strip(),
        "summary_vague":   data.get("summary_vague", "").strip(),
        "tags":            data.get("tags", []),
        "timestamp":       time.time(),
    }
    if "day" in data:
        try:
            event["day"] = int(data["day"])
        except (TypeError, ValueError):
            pass

    cdir = get_campaign_dir()
    events = load_chronicle(cdir)
    events.append(event)
    events = save_chronicle(cdir, events)

    logging.info(
        f"CHRONICLE: Recorded '{summary[:60]}' "
        f"(radius={radius}, factions_full={factions_full})"
    )
    return jsonify({"status": "ok", "total_events": len(events)})


@app.route('/regenerate_profile', methods=['POST'])
def regenerate_profile_route():
    """Evolves an NPC's Personality, Backstory, and SpeechQuirks based on their
    conversation history with the player. Preserves all other profile fields including Traits.
    POST body: {"sid": "<npc_storage_id>"}
    """
    logging.info("ROUTE: /regenerate_profile [POST]")
    data = request.json or {}
    sid = data.get("sid")
    if not sid:
        return jsonify({"status": "error", "message": "Missing NPC ID (sid)"}), 400

    # Resolve by storage ID — gateway handles name-to-entity resolution.
    char_data = character_gateway.read(sid, sid, ACTIVE_CAMPAIGN)
    if not char_data:
        return jsonify({"status": "error", "message": "Profile not found"}), 404

    real_name = char_data.get("Name", sid)
    real_faction = char_data.get("Faction", "")
    char_data = get_character_data(real_name, {"faction": real_faction, "persistent_id": sid}, char_id=sid, skip_generate=True)
    if not char_data:
        return jsonify({"status": "error", "message": "Profile not found"}), 404

    history = char_data.get("ConversationHistory", [])
    if not history:
        return jsonify({"status": "error",
                        "message": "No conversation history. Talk to this NPC first."}), 400

    name        = char_data.get("Name", sid)
    race        = char_data.get("Race", "Unknown")
    faction     = char_data.get("Faction", "Unknown")
    personality = char_data.get("Personality", "Unknown")
    backstory   = char_data.get("Backstory", "Unknown")
    persona_category = get_persona_category(
        race,
        faction,
        name=name,
        source="regeneration",
    )

    # Cap history sent to LLM to avoid token overflow; full history stays in the file
    _regen_history_limit = max(1, int(load_settings().get("profile_regen_history_lines", 80)))
    history_block = "\n".join(history[-_regen_history_limit:])

    system_msg = ("You are an expert on Kenshi lore and character growth. "
                  "You write NPC profiles in a grounded, cynical tone. "
                  "You ALWAYS respond ONLY with a valid JSON object.")
    if KAYAK_ENABLED:
        try:
            _npc_facts = (
                f"Name: {name}\nRace: {race}\n"
                f"Faction: {faction}\nOrigin Faction: {char_data.get('OriginFaction', 'Unknown')}\n"
                f"Job: {char_data.get('Job', 'None')}"
            )
            _bio_regen = kayak.build_biography_prompt(
                _npc_facts,
                campaign=ACTIVE_CAMPAIGN,
                race=race,
                persona_category=persona_category,
            )
            if _bio_regen:
                system_msg = _bio_regen
        except Exception as _regen_prompt_err:
            logging.warning(f"KAYAK: regeneration biography prompt failed for {name} ({_regen_prompt_err})")
    user_msg = f"""Rewrite the Personality and Backstory for the Kenshi NPC "{name}" based on their conversation history.

CURRENT PROFILE:
Personality: {personality}
Backstory: {backstory}
Race: {race} | Faction: {faction}

CONVERSATION HISTORY:
{history_block}

Instructions:
- EVOLVE the profile to reflect their experiences with the player.
- Maintain the Kenshi world's grounded, cynical tone.
- If they've bonded with the player, reflect that. If there was conflict, reflect that too.
- Response MUST be ONLY a JSON object with keys: "Personality", "Backstory", "SpeechQuirks"."""

    logging.info(f"REGEN: Evolving profile for {name} ({len(history)} history lines)...")

    try:
        regen_messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg}
        ]
        log_prompt_snapshot("profile_regeneration", messages=regen_messages, metadata={"npc": name, "campaign": ACTIVE_CAMPAIGN, "history_lines": len(history)})
        response_text = call_llm(
            regen_messages,
            max_tokens=int(load_settings().get("profile_regen_max_tokens", 1500)), temperature=float(load_settings().get("profile_temperature", 0.7))  # added by Pineaxe v07 - configurable via settings.json
        )

        if not response_text or "Empty Response" in response_text:
            return jsonify({"status": "error",
                            "message": "LLM returned an empty response. Try again or use an NPC with fewer memories."}), 500

        result = robust_json_parse(response_text)
        if not result:
            logging.error(f"REGEN: JSON parse failed for {name}. Raw: {response_text[:300]}")
            return jsonify({"status": "error", "message": "LLM response was not valid JSON. Try again."}), 500

        # Update only the three narrative fields; Traits and all metadata are preserved
        char_data["Personality"]  = result.get("Personality", personality)
        char_data["Backstory"]    = result.get("Backstory", backstory)
        char_data["SpeechQuirks"] = result.get("SpeechQuirks", char_data.get("SpeechQuirks", ""))
        char_data["_persona_category"] = persona_category
        _mark_profile_ready(char_data)

        character_gateway.write(char_data, ACTIVE_CAMPAIGN, reason="regeneration")

        def _wrap_profile_text(text):
            if not text:
                return ""
            paragraphs = str(text).split('\n')
            wrapped = []
            for p in paragraphs:
                if not p.strip():
                    wrapped.append("")
                    continue
                wrapped.extend(textwrap.wrap(p, width=110))
            return "\n".join(wrapped)

        formatted_profile = "\n".join([
            f"--- PROFILE: {name} ---",
            f"Faction: {char_data.get('Faction', 'Unknown')} | Race: {char_data.get('Race', 'Unknown')}",
            generate_relation_bar(char_data.get("Relation", 0)),
            "-" * 30,
            "PERSONALITY:",
            _wrap_profile_text(char_data.get("Personality", "")),
            "",
            "BACKSTORY:",
            _wrap_profile_text(char_data.get("Backstory", "")),
            "-" * 30,
            f"CONVERSATION LOG (Showing last 250 of {len(history)} lines):",
        ])

        logging.info(f"REGEN: Successfully evolved profile for {name}.")
        return jsonify({
            "status": "ok",
            "message": f"Successfully evolved {name}'s profile.",
            "name": name,
            "sid": char_data.get("ID", sid),
            "Personality": char_data.get("Personality", ""),
            "Backstory": char_data.get("Backstory", ""),
            "SpeechQuirks": char_data.get("SpeechQuirks", ""),
            "personality": char_data.get("Personality", ""),
            "backstory": char_data.get("Backstory", ""),
            "speech_quirks": char_data.get("SpeechQuirks", ""),
            "text": formatted_profile,
            "profile": {
                "name": name,
                "sid": char_data.get("ID", sid),
                "Personality": char_data.get("Personality", ""),
                "Backstory": char_data.get("Backstory", ""),
                "SpeechQuirks": char_data.get("SpeechQuirks", ""),
                "personality": char_data.get("Personality", ""),
                "backstory": char_data.get("Backstory", ""),
                "speech_quirks": char_data.get("SpeechQuirks", ""),
            }
        })

    except Exception as e:
        logging.error(f"REGEN: Failed for {sid}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/chat', methods=['POST'])
def chat():
    global CURRENT_MODEL_KEY
    data = request.json
    _drain_auto_synthesis_if_pending()
    debug_logger.debug(f"ROUTE: /chat [POST] (Request details omitted for security)")
    is_incapacitated = data.get("is_incapacitated", False)
    if is_incapacitated:
        logging.info(f"Target {data.get('npc', 'Unknown')} is incapacitated. Proceeding due to force-speech override.")
    if not data: return jsonify({"text": "Error: No JSON data provided"}), 400
    
    # Parse comma-separated NPC names and stabilize IDs
    raw_npc = data.get('npc', 'Someone')
    raw_npcs = data.get('npcs', [])
    
    # Stabilize name-to-id mapping for resolution accuracy
    name_to_id = {}
    
    def register(raw):
        if not raw: return ""
        raw_str = str(raw).strip()
        clean = _clean_npc_name(raw_str)
        if not clean:
            return ""
        name_to_id[clean] = raw_str if '|' in raw_str else clean
        return clean

    primary_npc = register(raw_npc)
    npcs = [register(n) for n in raw_npcs]
    
    # Ensure primary_npc is logic-ready
    player_name = get_effective_player_name(data.get('player', 'Drifter'))
    player_name_clean = _clean_npc_name(player_name)
    # Snapshot once so the whole request describes a single speaker, even if the
    # player reselects mid-flight. World state (time, environment, events) still
    # comes straight from PLAYER_CONTEXT — only the character record is overlaid.
    _eff_player = get_effective_player_context()
    mode = data.get('mode', 'talk')
    
    # 3. Update LIVE_CONTEXTS from provided nearby data (ensures reactions work immediately)
    nearby = data.get('nearby', [])
    # Keep a copy for /speaker: this payload carries squad members' ids even when
    # the richer POST /context feed has not landed yet.
    remember_chat_nearby(nearby)
    if nearby:
        for n in nearby:
            name = _clean_npc_name(n.get('name'))
            if name:
                n_ctx = _parse_context_dict(n, fallback_name=name)
                runtime_id = str(n_ctx.get('runtime_id') or n.get('runtime_id') or n_ctx.get('id') or n.get('id') or "").strip() or None
                persistent_id = str(n_ctx.get('persistent_id') or n.get('persistent_id') or "").strip() or None
                stable_storage_id = n_ctx.get('storage_id')
                _, _existing = resolve_live_context(name=name, context=n_ctx, explicit_id=(runtime_id or stable_storage_id))
                self_marker = runtime_id or stable_storage_id or name
                live_payload = dict(_existing or {})
                live_payload.update(n_ctx)
                live_payload.update({
                    "id": runtime_id,
                    "runtime_id": runtime_id,
                    "persistent_id": persistent_id,
                    "storage_id": stable_storage_id,
                    "name": name,
                    "nearby": [
                        x for x in nearby
                        if (
                            str(x.get('runtime_id') or x.get('id') or "").strip()
                            or _preferred_storage_id(
                                _clean_npc_name(x.get('name')),
                                x.get('faction') or x.get('Faction') or x.get('origin_faction') or x.get('OriginFaction'),
                                x.get('storage_id'),
                                uid=x.get('persistent_id')
                            )
                            or _clean_npc_name(x.get('name'))
                        ) != self_marker
                    ],
                    "player_dist": n_ctx.get('dist', 999.0),
                    # Prefer fresh flags from the current payload; fall back to cache
                    # so trader/shopkeeper prompt context survives sparse updates.
                    "is_trader": bool(n_ctx.get("is_trader", (_existing or {}).get("is_trader", False))),
                    "in_shop": bool(n_ctx.get("in_shop", (_existing or {}).get("in_shop", False))),
                })
                store_live_context(live_payload, name=name, explicit_id=(runtime_id or stable_storage_id))
                name_to_id[name] = f"{name}|{runtime_id}" if runtime_id else name
                continue
    
    # Filter player out of available NPCs to avoid hallucinated PC responses
    npcs = [n for n in npcs if _clean_npc_name(n) != player_name_clean]
    if _clean_npc_name(primary_npc) == player_name_clean and len(npcs) > 0:
        primary_npc = npcs[0]
        
    player_message = data.get('message', '')
    with _DirectChatLease():
    
        # --- TEST COMMAND INTERCEPT ---
        if player_message.startswith('/'):
            cmd_parts = player_message[1:].split(' ', 1)
            cmd = cmd_parts[0].lower()
            args = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
        
            test_action = None
            if cmd == "help" or cmd == "commands":
                help_text = "[DEBUG] Available test commands:\n" + \
                            "/take, /attack, /follow, /idle, /patrol, /join, /leave, /free, /breakout,\n" + \
                            "/move, /movefast, /home, /shop, /raid [Town], /travel [Town], /medic, /rescue, /repair,\n" + \
                            "/notify [msg], /give_cats [n], /take_cats [n], /drop [item],\n" + \
                            "/take_item [item], /spawn [Templ|Name|Desc], /relations [Fact] [n], /task [TASK]"
                return jsonify({"text": help_text, "actions": []}), 200
            elif cmd == "db_originalname":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                _db_original_hint = _lookup_original_name_hint(_db_target, _db_id, ACTIVE_CAMPAIGN)
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200
                if not (KAYAK_ENABLED and kayak and hasattr(kayak, "get_npc_fields")):
                    if _db_original_hint:
                        _db_display = str(_db_target or "NPC").strip() or "NPC"
                        logging.info(f"DB CMD: originalname { _db_display } -> {_db_original_hint} [runtime]")
                        return jsonify({"text": f"[DB] {_db_display}'s original name: {_db_original_hint}", "actions": []}), 200
                    return jsonify({"text": "[DB] Original-name lookup is unavailable right now.", "actions": []}), 200
                try:
                    _db_fields = kayak.get_npc_fields(_db_target, _db_id, ACTIVE_CAMPAIGN)
                except Exception as _db_err:
                    logging.warning(f"DB CMD: originalname lookup failed ({_db_err})")
                    _db_fields = {}
                if not _db_fields and not _db_original_hint:
                    return jsonify({"text": f"[DB] No database entry found for {_db_target or 'that NPC'}.", "actions": []}), 200

                _db_display = str(_db_fields.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_original = str(_db_fields.get("original_name") or _db_original_hint or "").strip()
                if _db_original:
                    logging.info(f"DB CMD: originalname {_db_display} -> {_db_original}")
                    return jsonify({"text": f"[DB] {_db_display}'s original name: {_db_original}", "actions": []}), 200
                return jsonify({"text": f"[DB] {_db_display}: no original name recorded.", "actions": []}), 200
            elif cmd == "db_title":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                _db_title = str(args or "").strip()
                if not _db_title:
                    return jsonify({"text": "[DB] Usage: /db_title [Title]", "actions": []}), 200
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200
                if not _sanitize_db_list_value(_db_title):
                    return jsonify({"text": "[DB] That title is invalid.", "actions": []}), 200

                _db_target_info = _resolve_db_title_target(_db_target, _db_id, _db_context)
                _db_display = str(_db_target_info.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_faction = _sanitize_db_list_value(_db_target_info.get("faction"))
                if not _db_faction:
                    return jsonify({"text": f"[DB] {_db_display}: faction not found.", "actions": []}), 200

                try:
                    _db_result = _add_title_to_renaming_list(_db_title, _db_faction)
                except ValueError:
                    return jsonify({"text": "[DB] That title could not be saved.", "actions": []}), 200
                except Exception as _db_err:
                    logging.error(f"DB CMD: title add failed for {_db_title!r} ({_db_err})")
                    return jsonify({"text": "[DB] Failed to update renaming_list.txt.", "actions": []}), 200

                _db_section = str(_db_result.get("section") or _db_faction).strip() or _db_faction
                if _db_result.get("status") == "exists":
                    logging.info(f"DB CMD: title {_db_title!r} already listed under {_db_section!r}")
                    return jsonify({"text": f"[DB] '{_db_title}' is already listed under #{_db_section}.", "actions": []}), 200
                if _db_result.get("status") == "created_section":
                    logging.info(f"DB CMD: title {_db_title!r} added under new faction {_db_faction!r} for {_db_display}")
                    return jsonify({"text": f"[DB] Added '{_db_title}' under new faction #{_db_faction}.", "actions": []}), 200

                logging.info(f"DB CMD: title {_db_title!r} added under {_db_faction!r} for {_db_display}")
                return jsonify({"text": f"[DB] Added '{_db_title}' under #{_db_faction}.", "actions": []}), 200
            elif cmd == "db_species":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                _db_category = _normalize_db_species_category(args)
                if not _db_category:
                    return jsonify({"text": "[DB] Usage: /db_species [animals|ferals|machines|sapients]", "actions": []}), 200
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200

                _db_target_info = _resolve_db_species_target(_db_target, _db_id, _db_context)
                _db_display = str(_db_target_info.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_race = str(_db_target_info.get("race") or "").strip()
                _db_species_folder = _safe_species_folder_name(_db_race)
                if not _db_race or not _db_species_folder:
                    return jsonify({"text": f"[DB] {_db_display}: race not found.", "actions": []}), 200

                try:
                    _db_result = _ensure_species_scaffold(_db_race, _db_category, ACTIVE_CAMPAIGN)
                except ValueError:
                    return jsonify({"text": "[DB] That species scaffold could not be created.", "actions": []}), 200
                except Exception as _db_err:
                    logging.error(f"DB CMD: species scaffold failed for {_db_race!r} in {_db_category!r} ({_db_err})")
                    return jsonify({"text": "[DB] Failed to create species scaffold.", "actions": []}), 200

                if _db_result.get("status") == "exists":
                    logging.info(
                        f"DB CMD: species scaffold already existed for {_db_race!r} "
                        f"in {_db_category!r} ({ACTIVE_CAMPAIGN}/Template)"
                    )
                    return jsonify({
                        "text": f"[DB] {_db_race} already has { _db_category } scaffolds in {ACTIVE_CAMPAIGN} and Template.",
                        "actions": [],
                    }), 200

                logging.info(
                    f"DB CMD: species scaffold created for {_db_race!r} in {_db_category!r} "
                    f"({ACTIVE_CAMPAIGN}/Template) dirs={_db_result.get('created_dirs')} files={_db_result.get('created_files')}"
                )
                return jsonify({
                    "text": f"[DB] Created {_db_category}/{_db_species_folder} scaffolds in {ACTIVE_CAMPAIGN} and Template.",
                    "actions": [],
                }), 200
            elif cmd in ("db_race_biogen", "db_race_chatgen"):
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                _db_rule = _sanitize_db_species_rule(args)
                _db_group = "biography" if cmd == "db_race_biogen" else "chat"
                if not _db_rule:
                    _usage = "/db_race_biogen [description]" if cmd == "db_race_biogen" else "/db_race_chatgen [description]"
                    return jsonify({"text": f"[DB] Usage: {_usage}", "actions": []}), 200
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200

                _db_target_info = _resolve_db_species_target(_db_target, _db_id, _db_context)
                _db_display = str(_db_target_info.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_race = str(_db_target_info.get("race") or "").strip()
                _db_category = _resolve_db_species_bucket(_db_target_info, source=cmd)
                if not _db_race or not _safe_species_folder_name(_db_race):
                    return jsonify({"text": f"[DB] {_db_display}: race not found.", "actions": []}), 200
                if not _db_category:
                    return jsonify({"text": f"[DB] {_db_display}: species category not found.", "actions": []}), 200

                try:
                    _db_result = _append_species_rule(_db_race, _db_category, _db_group, _db_rule, ACTIVE_CAMPAIGN)
                except ValueError:
                    return jsonify({"text": "[DB] That race rule could not be saved.", "actions": []}), 200
                except Exception as _db_err:
                    logging.error(
                        f"DB CMD: race rule append failed for {_db_race!r} "
                        f"group={_db_group!r} category={_db_category!r} ({_db_err})"
                    )
                    return jsonify({"text": "[DB] Failed to update race rules.", "actions": []}), 200

                logging.info(
                    f"DB CMD: appended {_db_group} rule for race {_db_race!r} "
                    f"in {_db_category!r} ({ACTIVE_CAMPAIGN}/Template)"
                )
                return jsonify({
                    "text": f"[DB] Added {_db_group} rule to {_db_category}/{_db_result.get('species_folder')} in {ACTIVE_CAMPAIGN} and Template.",
                    "actions": [],
                }), 200
            elif cmd == "db_addrace_final":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                if args:
                    return jsonify({"text": "[DB] Usage: /db_addrace_final", "actions": []}), 200
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200

                _db_target_info = _resolve_db_species_target(_db_target, _db_id, _db_context)
                _db_display = str(_db_target_info.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_race = str(_db_target_info.get("race") or "").strip()
                if not _db_race or not _safe_species_folder_name(_db_race):
                    return jsonify({"text": f"[DB] {_db_display}: race not found.", "actions": []}), 200

                _db_category = _find_existing_species_category(_db_race, ACTIVE_CAMPAIGN)
                if not _db_category:
                    return jsonify({
                        "text": f"[DB] {_db_display}: no species folder found for race '{_db_race}'. Use /db_species first.",
                        "actions": [],
                    }), 200

                try:
                    _db_result = _add_race_to_persona_registry(_db_race, _db_category)
                except ValueError:
                    return jsonify({"text": "[DB] That race could not be added to persona_races.json.", "actions": []}), 200
                except Exception as _db_err:
                    logging.error(f"DB CMD: addrace_final failed for {_db_race!r} in {_db_category!r} ({_db_err})")
                    return jsonify({"text": "[DB] Failed to update persona_races.json.", "actions": []}), 200

                _db_keys = ", ".join(_db_result.get("keys") or [])
                if _db_result.get("status") == "exists":
                    logging.info(f"DB CMD: addrace_final {_db_race!r} already present in {_db_category!r} -> {_db_keys}")
                    return jsonify({
                        "text": f"[DB] {_db_race} is already present in persona_races.json ({_db_keys}).",
                        "actions": [],
                    }), 200

                logging.info(
                    f"DB CMD: addrace_final added {_db_race!r} to persona_races.json "
                    f"category={_db_category!r} keys={_db_keys!r}"
                )
                return jsonify({
                    "text": f"[DB] Added {_db_race} to persona_races.json ({_db_keys}).",
                    "actions": [],
                }), 200
            elif cmd == "db_clear_dialogue":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200
                if not (KAYAK_ENABLED and kayak):
                    return jsonify({"text": "[DB] Dialogue clear is unavailable right now.", "actions": []}), 200

                try:
                    _db_fields = kayak.get_npc_fields(_db_target, _db_id, ACTIVE_CAMPAIGN) or {}
                except Exception as _db_err:
                    logging.warning(f"DB CMD: clear_dialogue lookup failed ({_db_err})")
                    _db_fields = {}
                if not _db_fields:
                    return jsonify({"text": f"[DB] No database entry found for {_db_target or 'that NPC'}.", "actions": []}), 200

                _db_display = str(_db_fields.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_lookup_name = str(_db_fields.get("_entity_name") or _db_display or _db_target or "").strip() or _db_target
                _db_resolved_id = str(
                    _db_id
                    or _db_fields.get("persistent_id")
                    or _db_fields.get("runtime_id")
                    or ""
                ).strip() or None
                _db_name_for_cache = str(_db_display or _db_target or "").strip()
                _db_ok = character_gateway.write_dialogue_only(
                    _db_lookup_name,
                    _db_resolved_id,
                    [],
                    ACTIVE_CAMPAIGN,
                )
                if not _db_ok:
                    return jsonify({"text": f"[DB] Failed to clear dialogue for {_db_display}.", "actions": []}), 200

                _invalidate_character_cache_aliases(_db_name_for_cache, _db_target, persistent_id=_db_resolved_id)
                logging.info(f"DB CMD: cleared dialogue history for {_db_display!r}")
                return jsonify({"text": f"[DB] Cleared dialogue history for {_db_display}.", "actions": []}), 200
            elif cmd == "db_clear_entity":
                _db_context = data.get('context', '')
                _db_id = extract_id_from_context(_db_context)
                _db_target = primary_npc
                if (not _db_target or _clean_npc_name(_db_target) in ("", "someone")) and not _db_id:
                    return jsonify({"text": "[DB] No NPC selected.", "actions": []}), 200
                if not (KAYAK_ENABLED and kayak):
                    return jsonify({"text": "[DB] Entity clear is unavailable right now.", "actions": []}), 200

                try:
                    _db_fields = kayak.get_npc_fields(_db_target, _db_id, ACTIVE_CAMPAIGN) or {}
                except Exception as _db_err:
                    logging.warning(f"DB CMD: clear_entity lookup failed ({_db_err})")
                    _db_fields = {}
                if not _db_fields:
                    return jsonify({"text": f"[DB] No database entry found for {_db_target or 'that NPC'}.", "actions": []}), 200

                _db_display = str(_db_fields.get("display_name") or _db_target or "NPC").strip() or "NPC"
                _db_lookup_name = str(_db_fields.get("_entity_name") or _db_display or _db_target or "").strip() or _db_target
                _db_resolved_id = str(
                    _db_id
                    or _db_fields.get("persistent_id")
                    or _db_fields.get("runtime_id")
                    or ""
                ).strip() or None

                try:
                    _db_result = kayak.clear_npc_generated_profile_fields(_db_lookup_name, _db_resolved_id, ACTIVE_CAMPAIGN)
                except Exception as _db_err:
                    logging.error(f"DB CMD: clear_entity failed for {_db_display!r} ({_db_err})")
                    return jsonify({"text": "[DB] Failed to clear generated entity prose.", "actions": []}), 200

                if _db_result.get("error"):
                    logging.warning(f"DB CMD: clear_entity error for {_db_display!r}: {_db_result.get('error')}")
                    return jsonify({"text": f"[DB] Failed to clear generated entity prose for {_db_display}.", "actions": []}), 200

                _db_cleared = list(_db_result.get("cleared_fields") or [])
                _invalidate_character_cache_aliases(_db_display, _db_target, persistent_id=_db_resolved_id)
                if not _db_cleared:
                    logging.info(f"DB CMD: no generated prose fields found for {_db_display!r}")
                    return jsonify({"text": f"[DB] {_db_display}: no generated prose fields found.", "actions": []}), 200

                logging.info(f"DB CMD: cleared generated prose fields for {_db_display!r}: {_db_cleared}")
                return jsonify({"text": f"[DB] Cleared generated entity prose for {_db_display}.", "actions": []}), 200
            
            if cmd == "attack": test_action = "[ATTACK]"
            elif cmd == "follow": test_action = "[ACTION: FOLLOW_PLAYER]"
            elif cmd == "idle": test_action = "[ACTION: IDLE]"
            elif cmd == "patrol": test_action = "[ACTION: PATROL_TOWN]"
            elif cmd == "join": test_action = "[ACTION: JOIN_PARTY]"
            elif cmd == "leave": test_action = "[ACTION: LEAVE]"
            elif cmd == "free": test_action = "[ACTION: FREE_PLAYER]"
            elif cmd == "breakout": test_action = "[ACTION: BREAKOUT_PLAYER]"
            elif cmd == "move": test_action = "[ACTION: MOVE_ON_FREE_WILL]"
            elif cmd == "movefast": test_action = "[ACTION: MOVE_ON_FREE_WILL_FAST]"
            elif cmd == "home": test_action = "[ACTION: GO_HOMEBUILDING]"
            elif cmd == "shop": test_action = "[ACTION: STAND_AT_SHOPKEEPER_NODE]"
            elif cmd == "raid": test_action = f"[ACTION: RAID_TOWN: {args}]"
            elif cmd == "travel": test_action = f"[ACTION: TRAVEL_TO_TARGET_TOWN: {args}]"
            elif cmd == "medic": test_action = "[ACTION: JOB_MEDIC]"
            elif cmd == "rescue": test_action = "[ACTION: FIND_AND_RESCUE]"
            elif cmd == "repair": test_action = "[ACTION: JOB_REPAIR_ROBOT]"
            elif cmd == "notify": test_action = f"[ACTION: NOTIFY: {args}]"
            elif cmd == "give_cats": test_action = f"[ACTION: GIVE_CATS: {args}]"
            elif cmd == "take_cats": test_action = f"[ACTION: TAKE_CATS: {args}]"
            elif cmd == "take_item": test_action = f"[ACTION: TAKE_ITEM: {_normalize_item_arg_preserve_count(args)}]"
            elif cmd == "take":
                inv = _eff_player.get("inventory", [])
                if inv:
                    item_name = inv[0].get("name", "Unknown Item")
                    test_action = f"[ACTION: TAKE_ITEM: {item_name}]"
                else:
                    return jsonify({"text": "[DEBUG] Error: Player inventory is empty or unknown. Call /context to refresh.", "actions": []}), 200
            elif cmd == "drop": test_action = f"[ACTION: DROP_ITEM: {_normalize_item_arg_preserve_count(args)}]"
            elif cmd == "spawn": test_action = f"[ACTION: SPAWN_ITEM: {_normalize_spawn_args(args)}]"
            elif cmd == "relations":
                rparts = args.rsplit(' ', 1)
                if len(rparts) == 2:
                    test_action = f"[ACTION: FACTION_RELATIONS: {rparts[0].strip()}: {rparts[1].strip()}]"
            elif cmd == "task": test_action = f"[TASK: {args.upper()}]"
        
            if test_action:
                logging.info(f"TEST COMMAND: {cmd} -> {test_action}")
                return jsonify({
                    "text": f"[DEBUG] Executing test command: {test_action}",
                    "actions": [test_action]
                }), 200
            
        # /speaker — choose which of your own characters is talking.
        #
        # The stock DLL always reports squad 1 slot 1. The SelectedSpeaker plugin
        # fixes that properly by reading the in-game selection; this command is
        # the no-plugin path to the same override, so it deliberately writes to
        # the same state and loses to a live plugin report.
        #
        # Handled before the /k_ block because it must work with Kayak offline.
        if player_message.lower().startswith("/speaker"):
            _sp_arg = player_message[len("/speaker"):].strip()
            _roster = list_squad_names()
            _current = get_effective_player_name(player_name)

            if not _sp_arg:
                _lines = [f"Speaking as: {_current}"]
                if not selected_speaker_enabled():
                    _lines.append("(disabled: EnableSelectedSpeaker = 0)")
                if selected_is_fresh():
                    _lines.append("Set by the SelectedSpeaker plugin (live selection).")
                elif command_speaker_active():
                    _lines.append("Set by /speaker. Use '/speaker off' to revert.")
                else:
                    _lines.append("Default: first character of squad 1.")
                if _roster:
                    _lines.append("Squad: " + ", ".join(_roster))
                else:
                    _lines.append("Squad roster not received yet. Try again in a moment.")
                return jsonify({"text": "\n".join(_lines), "actions": []}), 200

            if _sp_arg.lower() in ("off", "reset", "none", "stock", "default"):
                _was = clear_speaker_command()
                _msg = (f"Speaker reset to the default (was {_was})."
                        if _was else "Speaker was already the default.")
                logging.info("SPEAKER: cleared by command")
                return jsonify({"text": _msg, "actions": []}), 200

            _status, _resolved = set_speaker_by_name(_sp_arg)

            if _status == "not_in_squad":
                _hint = ("Squad: " + ", ".join(_roster)) if _roster else \
                        "No squad roster received yet. Try again in a moment."
                return jsonify({
                    "text": f"No squad member matches '{_sp_arg}'. {_hint}",
                    "actions": [],
                }), 200

            if _status == "not_nearby":
                return jsonify({
                    "text": (f"{_resolved} is not close enough. The game only sends "
                             f"details for characters near you, so they would have "
                             f"to borrow someone else's body. Bring them closer and "
                             f"try again."),
                    "actions": [],
                }), 200

            if _status == "reverted":
                logging.info("SPEAKER: reverted to the stock speaker by command")
                return jsonify({
                    "text": f"Speaking as {_resolved}. That is already the default speaker.",
                    "actions": [],
                }), 200

            # Log the handles too: Kayak resolves the player entity by id before
            # name, so a stale id here is the difference between the chosen
            # character and someone else's personality showing up in the prompt.
            _sp_ctx = get_effective_player_context()
            _sp_ids = {k: _sp_ctx.get(k) or "" for k in
                       ("persistent_id", "runtime_id", "id", "storage_id")}
            logging.info(
                f"SPEAKER: now speaking as {_resolved!r} (set by command); "
                f"ids={_sp_ids}; "
                f"race={_sp_ctx.get('race')!r} gender={_sp_ctx.get('gender')!r}"
            )
            _note = ""
            if selected_is_fresh():
                _note = ("\nNote: the SelectedSpeaker plugin is running and takes "
                         "priority, so the in-game selection still wins.")
            return jsonify({
                "text": f"Now speaking as {_resolved}.{_note}",
                "actions": [],
            }), 200

        # added by Pineaxe v04 - /k_ commands: in-game NPC entity editor via Kayak
        # /k_speak does NOT return early - sets a flag and falls through to the LLM call.
        # All other direct commands return early with a confirmation bubble.
        _k_speak_mode = False
        _k_speak_line = ""
        _music_cmds = {
            "help", "play", "folder", "pause", "resume", "stop",
            "next", "prev", "vol", "playlist", "find",
            "shuffle", "loop", "reload", "status",
        }
        _legacy_music_cmds = {
            "mhelp": "help",
            "mplay": "play",
            "mfolder": "folder",
            "mpause": "pause",
            "mresume": "resume",
            "mstop": "stop",
            "mnext": "next",
            "mprev": "prev",
            "mvol": "vol",
            "mplaylist": "playlist",
            "mfind": "find",
            "mshuffle": "shuffle",
            "mloop": "loop",
            "mreload": "reload",
            "mstatus": "status",
        }

        if KAYAK_ENABLED and player_message.lower().startswith("/m_"):
            _mparts = player_message[3:].split(" ", 1)
            _mcmd = _mparts[0].lower().strip()
            _margs = _mparts[1].strip() if len(_mparts) > 1 else ""
            if _mcmd in _music_cmds:
                _music_cmd = f"/m_{_mcmd}"
                if _margs:
                    _music_cmd += f" {_margs}"
                _msg = kayak.k_mdispatch(_music_cmd)
                logging.info(f"MUSIC CMD: m_{_mcmd} -> {_margs[:80]!r}" if _margs else f"MUSIC CMD: m_{_mcmd}")
                return jsonify({"text": _msg, "actions": []}), 200

        if KAYAK_ENABLED and player_message.lower().startswith("/k_"):
            _kparts = player_message[3:].split(" ", 1)
            _kcmd   = _kparts[0].lower().strip()
            _kargs  = _kparts[1].strip() if len(_kparts) > 1 else ""
            _knpc   = primary_npc
            _kid    = None
            _kcamp  = ACTIVE_CAMPAIGN

            if _kcmd == "help":
                return jsonify({"text": kayak.k_help(), "actions": []}), 200

            elif _kcmd in _legacy_music_cmds:
                _music_cmd = f"/m_{_legacy_music_cmds[_kcmd]}"
                if _kargs:
                    _music_cmd += f" {_kargs}"
                _msg = kayak.k_mdispatch(_music_cmd)
                logging.info(f"KAYAK CMD: {_kcmd} -> {_kargs[:80]!r}" if _kargs else f"KAYAK CMD: {_kcmd}")
                return jsonify({"text": _msg, "actions": []}), 200

            elif _kcmd == "visited" and _kargs:
                _msg = kayak.k_visited(_knpc, _kargs, _kid, _kcamp)
                logging.info(f"KAYAK CMD: k_visited {_knpc} -> {_kargs}")
                return jsonify({"text": _msg, "actions": []}), 200

            elif _kcmd == "job" and _kargs:
                _msg = kayak.k_job(_knpc, _kargs, _kid, _kcamp)
                logging.info(f"KAYAK CMD: k_job {_knpc} -> {_kargs}")
                return jsonify({"text": _msg, "actions": []}), 200

            elif _kcmd == "backstory" and _kargs:
                _msg = kayak.k_backstory(_knpc, _kargs, _kid, _kcamp)
                logging.info(f"KAYAK CMD: k_backstory {_knpc}")
                return jsonify({"text": _msg, "actions": []}), 200

            elif _kcmd == "speech" and _kargs:
                _msg = kayak.k_speech(_knpc, _kargs, _kid, _kcamp)
                logging.info(f"KAYAK CMD: k_speech {_knpc}")
                return jsonify({"text": _msg, "actions": []}), 200

            elif _kcmd == "note" and _kargs:
                _note_parts = _kargs.split(" ", 1)
                if len(_note_parts) == 2:
                    _msg = kayak.k_note(_knpc, _note_parts[0], _note_parts[1], _kid, _kcamp)
                    logging.info(f"KAYAK CMD: k_note {_knpc} {_note_parts[0]}")
                    return jsonify({"text": _msg, "actions": []}), 200
                else:
                    return jsonify({"text": "[KAYAK] Usage: /k_note <field> <value>", "actions": []}), 200

            elif _kcmd == "npc_prices" and _kargs:
                try:
                    _mult = float(_kargs)
                    _msg  = kayak.k_npc_prices(_knpc, _mult, _kid, _kcamp)
                    logging.info(f"KAYAK CMD: k_npc_prices {_knpc} -> {_mult}")
                    return jsonify({"text": _msg, "actions": []}), 200
                except ValueError:
                    return jsonify({"text": "[KAYAK] Usage: /k_npc_prices <multiplier>", "actions": []}), 200

            elif _kcmd == "global_prices" and _kargs:
                try:
                    _mult = float(_kargs)
                    kayak.set_global_price_modifier(_mult)
                    logging.info(f"KAYAK CMD: k_global_prices -> {_mult}")
                    return jsonify({"text": f"[KAYAK] Global prices set to x{_mult}.", "actions": []}), 200
                except ValueError:
                    return jsonify({"text": "[KAYAK] Usage: /k_global_prices <multiplier>", "actions": []}), 200

            elif _kcmd == "city_prices" and _kargs:
                try:
                    _city_parts = _kargs.rsplit(" ", 1)
                    if len(_city_parts) == 2:
                        _city, _mult = _city_parts[0].strip(), float(_city_parts[1])
                        kayak.set_city_price_modifier(_city, _mult, _kcamp)
                        logging.info(f"KAYAK CMD: k_city_prices {_city} -> {_mult}")
                        return jsonify({"text": f"[KAYAK] {_city} prices set to x{_mult}.", "actions": []}), 200
                    else:
                        return jsonify({"text": "[KAYAK] Usage: /k_city_prices <city> <multiplier>", "actions": []}), 200
                except ValueError:
                    return jsonify({"text": "[KAYAK] Usage: /k_city_prices <city> <multiplier>", "actions": []}), 200

            elif _kcmd == "clean":
                _clean_result = _cleanup_pending_profiles(reason="manual")
                logging.info(f"KAYAK CMD: k_clean removed {_clean_result['deleted']} pending NPCs")
                return jsonify({
                    "text": (
                        f"[KAYAK] Cleaned {_clean_result['deleted']} pending NPCs. "
                        f"Kept {_clean_result['kept']} entries and preserved {_clean_result['favorites']} favorites."
                    ),
                    "actions": []
                }), 200

            elif _kcmd == "speak" and _kargs:
                # added by Pineaxe v07 - echo line directly as NPC bubble, no LLM needed.
                # Extended: if the line contains [ACTION: ...] or [TASK: ...], return
                # as a parsable action set so tests can bypass the LLM.
                action_pattern = re.compile(r"\[(?:ACTION|TASK):[^\]]+\]")
                actions = [_normalize_direct_action_tag(a) for a in action_pattern.findall(_kargs)]
                speech_line = action_pattern.sub("", _kargs).strip() or _kargs
                logging.info(f"KAYAK CMD: k_speak {_knpc} -> {_kargs[:60]!r}")
                try:
                    _speak_context = data.get('context', '')
                    _speak_char_id = extract_id_from_context(_speak_context)
                    _speak_data = get_character_data(_knpc, _speak_context, char_id=_speak_char_id)
                    _speak_sid = _speak_data.get("ID") or _knpc
                    _speak_time = get_current_time_prefix()
                    _speak_pname = get_effective_player_name(data.get('player', 'Drifter'))
                    _speak_data["ConversationHistory"].append(f"{_speak_time}{_speak_pname}: /k_speak")
                    _speak_data["ConversationHistory"].append(f"{_speak_time}{_knpc}: {speech_line}")
                    if len(_speak_data["ConversationHistory"]) > DIALOGUE_HISTORY_LIMIT:
                        _speak_data["ConversationHistory"] = _speak_data["ConversationHistory"][-DIALOGUE_HISTORY_LIMIT:]
                    _, _speak_live_ctx = resolve_live_context(
                        name=_knpc,
                        context=_speak_data,
                        explicit_id=_speak_data.get("ID") or _speak_sid,
                    )
                    _speak_live_ctx = _speak_live_ctx or {}
                    _speak_hydration_job = _load_hydration_job(
                        name=_knpc,
                        storage_id=_speak_sid,
                        campaign=ACTIVE_CAMPAIGN,
                    )
                    if _profile_is_hydrating(_speak_data) or _speak_hydration_job:
                        _synced_job = _sync_hydration_job(
                            _speak_data,
                            name=_knpc,
                            storage_id=_speak_sid,
                            campaign=ACTIVE_CAMPAIGN,
                            live_ctx=_speak_live_ctx,
                        )
                        _hydrated, _hydrate_ok = _run_hydration_job(
                            _synced_job,
                            campaign=ACTIVE_CAMPAIGN,
                            live_ctx=_speak_live_ctx,
                        ) if _synced_job else (None, False)
                        if _hydrate_ok and _hydrated:
                            _pending_discard(
                                name=_knpc,
                                context=_speak_context,
                                explicit_id=_speak_char_id,
                                live_ctx=_speak_live_ctx,
                                storage_id=_speak_sid,
                                profile=_hydrated,
                            )
                    elif should_save_profile(_knpc, _speak_sid, _speak_data):
                        character_gateway.write_dialogue_only(
                            _knpc, _speak_sid,
                            _speak_data["ConversationHistory"],
                            campaign=ACTIVE_CAMPAIGN,
                        )
                except Exception as _speak_err:
                    logging.warning(f"KAYAK k_speak: history save failed ({_speak_err})")
                return jsonify({"text": speech_line, "actions": actions}), 200

            elif _kcmd == "speak":
                return jsonify({"text": "[KAYAK] Usage: /k_speak <line>  e.g. /k_speak Hey Tealc, how was the patrol?", "actions": []}), 200

            else:
                return jsonify({"text": f"[KAYAK] Unknown command: /k_{_kcmd}. Type /k_help for the list.", "actions": []}), 200

        event = data.get('event')
    
        # Ignore internal events that aren't chat prompts
        if event == "selection_clear":
            return jsonify({"status": "ignored"}), 200
        
        # Prevent unprompted generation if no message is provided (unless it's an ambient event)
        if not player_message and event != "ambient_flavor":
            return jsonify({"text": "...", "actions": []}), 200
    
        # Handle Ambient Flavor (NPC to NPC chat)
        is_ambient = event == "ambient_flavor"
        if is_ambient:
            player_message = "[AMBIENT CONVERSATION TRIGGERED]"
        else:
            pass
        
        context = data.get('context', '')
        primary_ref = name_to_id.get(primary_npc, primary_npc)
        if primary_npc and not is_ambient:
            resolved_name, resolved_context, resolved_id, resolved_ref = resolve_primary_target(
                raw_npc,
                context=context,
                nearby_data=nearby,
                mode=mode,
            )
            if resolved_name:
                primary_npc = resolved_name
            if resolved_context is not None:
                context = resolved_context
            primary_id = resolved_id or extract_id_from_context(context)
            primary_ref = resolved_ref or name_to_id.get(primary_npc, primary_npc)
        else:
            primary_id = extract_id_from_context(context)

        primary_ctx_dict = {}

        def _refresh_primary_tracking():
            nonlocal context, primary_id, primary_ref, primary_ctx_dict

            if primary_npc:
                name_to_id[primary_npc] = primary_ref or name_to_id.get(primary_npc, primary_npc) or primary_npc

            if primary_npc and context:
                try:
                    ctx_dict = json.loads(context) if isinstance(context, str) else context
                    if ctx_dict:
                        runtime_hint = str(ctx_dict.get("runtime_id") or ctx_dict.get("id") or "").strip() or None
                        if primary_id and _is_strong_uid(primary_id) and "persistent_id" not in ctx_dict:
                            ctx_dict["persistent_id"] = primary_id
                        elif primary_id and not runtime_hint:
                            ctx_dict["id"] = primary_id
                        store_live_context(ctx_dict, name=primary_npc, explicit_id=(runtime_hint or primary_id))
                        primary_ctx_dict = dict(ctx_dict)
                    else:
                        primary_ctx_dict = {}
                except Exception as e:
                    primary_ctx_dict = {}
                    logging.error(f"Error registering primary context: {e}")
            else:
                primary_ctx_dict = {}

        _refresh_primary_tracking()
    
        # radii
        whisper_radius, talk_radius, yell_radius = get_config_radii()
    
        npcs_in_radius = []
        # USE THE ROOT NEARBY LIST FOR ACCURATE PROXIMITY DETECTION
        nearby_data = data.get('nearby', [])
        primary_summary = _context_identity_summary(context, fallback_name=primary_npc) if primary_npc else {}
        primary_markers = set(
            filter(
                None,
                [
                    primary_summary.get("runtime_id"),
                    primary_summary.get("storage_id"),
                    primary_summary.get("key"),
                ],
            )
        )
        if not primary_markers and primary_npc:
            primary_markers = {primary_npc}
        for n in nearby_data:
            n_ctx = _parse_context_dict(n, fallback_name=n.get("name", ""))
            name = _clean_npc_name(n_ctx.get("name") or n.get("name"))
            if not name or name == player_name:
                continue
            if context_is_dead(n_ctx):
                logging.info(f"YELL FILTER: skipped dead nearby NPC '{name}'")
                continue

            n_markers = set(
                filter(
                    None,
                    [
                        str(n_ctx.get("id") or n.get("id") or "").strip(),
                        _preferred_storage_id(
                            name,
                            n_ctx.get("faction") or n_ctx.get("Faction") or n_ctx.get("origin_faction") or n_ctx.get("OriginFaction"),
                            n_ctx.get("storage_id"),
                            n.get("storage_id"),
                        ),
                    ],
                )
            )
            if not n_markers:
                n_markers = {name}
            if primary_markers.intersection(n_markers):
                continue

            try:
                dist = float(n_ctx.get("dist", n.get("dist", 999.0)) or 999.0)
            except Exception:
                dist = 999.0
            # Check if they are in radius based on communication mode
            if mode == "whisper":
                # Whisper is one-on-one, no one eavesdrops in this mode now
                continue 
            elif mode == "talk":
                if dist <= talk_radius: npcs_in_radius.append(name)
            elif mode == "yell":
                if dist <= yell_radius: npcs_in_radius.append(name)

        # 4. History Update (Overhearing)
    
        def get_local_context_and_id(target_name):
            # Clean target_name for comparison
            clean_target = _clean_npc_name(target_name)
            target_id = target_name.split('|', 1)[1].strip() if '|' in target_name else None

            # Player self-history path: ensure the player profile receives their own
            # spoken lines in normal AI chat (not just overheard listeners).
            if clean_target and clean_target == player_name_clean:
                p_ctx = dict(_eff_player)
                if p_ctx:
                    p_ctx.setdefault("name", player_name)
                    p_sid = (
                        p_ctx.get("persistent_id")
                        or p_ctx.get("runtime_id")
                        or p_ctx.get("id")
                        or _preferred_storage_id(
                            player_name,
                            p_ctx.get("faction"),
                            p_ctx.get("storage_id"),
                            uid=p_ctx.get("persistent_id"),
                        )
                    )
                    return json.dumps(p_ctx), p_sid
                return "", None
        
            if clean_target == primary_npc and (not target_id or str(target_id) == str(primary_id)):
                return context, primary_id
            
            # Check current request's nearby data first (highest accuracy)
            nearby_data = data.get('nearby', [])
            for n in nearby_data:
                n_ctx = _parse_context_dict(n, fallback_name=n.get("name", ""))
                n_name = _clean_npc_name(n_ctx.get("name", n.get("name", "")))
                clean_n = n_name
                n_runtime = n_ctx.get("runtime_id") or n_ctx.get("id")
                n_sid = n_ctx.get("persistent_id") or n_ctx.get("storage_id") or n_runtime
                if target_id and str(n_runtime or n_sid) == str(target_id):
                    return json.dumps(n_ctx), (n_runtime or n_sid)
                if clean_n == clean_target:
                    return json.dumps(n_ctx), (n_runtime or n_sid)

            _, cached_ctx = resolve_live_context(name=clean_target, explicit_id=target_id)
            if cached_ctx:
                return json.dumps(cached_ctx), (
                    cached_ctx.get("runtime_id")
                    or cached_ctx.get("persistent_id")
                    or cached_ctx.get("storage_id")
                    or cached_ctx.get("id")
                )
                
            return "", None

        # Determine listeners (everyone in radius)
        # Ensure listeners are clean names for logic processing
        raw_listeners = list(set([primary_npc] + npcs_in_radius))
        listeners = []
        for l in raw_listeners:
            clean_l = _clean_npc_name(l)
            if clean_l not in listeners: listeners.append(clean_l)

        # Persistence targets include nearby listeners plus the player profile.
        save_listeners = list(listeners)
        if player_name_clean and player_name_clean not in save_listeners:
            save_listeners.append(player_name_clean)

        _yell_speaker_persona_cache = {}

        def _yell_speaker_persona_details(speaker_name):
            speaker_clean = _clean_npc_name(speaker_name)
            if not speaker_clean:
                return "unknown", "Unknown", ""

            cached = _yell_speaker_persona_cache.get(speaker_clean)
            if cached:
                return cached

            speaker_ctx, speaker_local_id = get_local_context_and_id(speaker_clean)
            speaker_ctx_dict = _parse_context_dict(speaker_ctx, fallback_name=speaker_clean)
            _, speaker_live_ctx = resolve_live_context(
                name=speaker_clean,
                context=speaker_ctx_dict,
                explicit_id=speaker_local_id,
            )
            speaker_live_ctx = speaker_live_ctx or {}

            speaker_profile = {}
            try:
                speaker_storage_id = (
                    speaker_ctx_dict.get("storage_id")
                    or speaker_live_ctx.get("storage_id")
                    or make_storage_id(
                        speaker_clean,
                        speaker_ctx_dict.get("faction") or speaker_live_ctx.get("faction", ""),
                        context=speaker_ctx_dict or speaker_live_ctx,
                    )
                )
                if speaker_storage_id:
                    speaker_profile = character_gateway.read(speaker_clean, speaker_storage_id, ACTIVE_CAMPAIGN) or {}
            except Exception:
                speaker_profile = {}

            speaker_race = (
                speaker_ctx_dict.get("race")
                or speaker_live_ctx.get("race")
                or speaker_profile.get("Race")
                or "Unknown"
            )
            speaker_faction = (
                speaker_ctx_dict.get("faction")
                or speaker_ctx_dict.get("Faction")
                or speaker_live_ctx.get("faction")
                or speaker_live_ctx.get("Faction")
                or speaker_profile.get("Faction")
                or speaker_profile.get("OriginFaction")
                or ""
            )
            speaker_category = get_persona_category(
                speaker_race,
                speaker_faction,
                name=speaker_clean,
                source="yell_speaker_filter",
            )
            cached = (speaker_category, speaker_race, speaker_faction)
            _yell_speaker_persona_cache[speaker_clean] = cached
            return cached

        def _is_sapient_yell_speaker(speaker_name):
            speaker_ctx, speaker_local_id = get_local_context_and_id(speaker_name)
            speaker_ctx_dict = _parse_context_dict(speaker_ctx, fallback_name=speaker_name)
            _, speaker_live_ctx = resolve_live_context(
                name=speaker_name,
                context=speaker_ctx_dict,
                explicit_id=speaker_local_id,
            )
            if context_is_dead(speaker_ctx_dict) or context_is_dead(speaker_live_ctx or {}):
                return False
            speaker_category, _, _ = _yell_speaker_persona_details(speaker_name)
            return speaker_category == "sapient"

        approved_yell_speakers = set()
        approved_yell_speaker_aliases = {}
        _recent_direct_chat_marked = False

        def _yell_speaker_alias_key(speaker_name: str) -> str:
            clean = _clean_npc_name(speaker_name)
            if not clean:
                return ""
            clean = re.sub(r'[\"“”‘’`]', '', clean)
            clean = re.sub(r'\s+', ' ', clean).strip().lower()
            return clean

        def _resolve_yell_speaker_name(speaker_name: str) -> str:
            clean = _clean_npc_name(speaker_name)
            if not clean:
                return ""
            if clean in approved_yell_speakers:
                return clean
            return approved_yell_speaker_aliases.get(_yell_speaker_alias_key(clean), clean)

        # 5. Determine who the LLM actually responds as
        if mode == 'yell':
            # Cap at 6 responders (1 primary + 5 others) to keep prompt under ~4000 tokens.
            # More than 6 voices adds marginal immersion but doubles generation time.
            _settings = load_settings()
            _yell_limit = max(1, int(_settings.get("yell_responder_limit", 6)))
            eligible_yell_speakers = [n for n in listeners if _is_sapient_yell_speaker(n)]

            if not _is_sapient_yell_speaker(primary_npc):
                if eligible_yell_speakers:
                    old_primary_npc = primary_npc
                    reanchored_primary = eligible_yell_speakers[0]
                    logging.info(
                        f"YELL TARGET: Re-anchored non-sapient primary '{old_primary_npc}' "
                        f"to nearby sapient '{reanchored_primary}'"
                    )
                    primary_npc = reanchored_primary
                    primary_ref = name_to_id.get(primary_npc, primary_npc)
                    reanchor_context, reanchor_id = get_local_context_and_id(primary_npc)
                    if reanchor_context:
                        context = reanchor_context
                    if reanchor_id:
                        primary_id = reanchor_id
                    _refresh_primary_tracking()
                    if not is_ambient and primary_npc:
                        mark_recent_direct_chat(primary_npc, context=context, explicit_id=primary_id)
                        _recent_direct_chat_marked = True
                else:
                    logging.info(
                        f"YELL TARGET: Keeping non-sapient primary '{primary_npc}' "
                        f"because no sapient yell speakers are nearby"
                    )
                    if not is_ambient and primary_npc:
                        mark_recent_direct_chat(primary_npc, context=context, explicit_id=primary_id)
                        _recent_direct_chat_marked = True

            eligible_others = [n for n in eligible_yell_speakers if n != primary_npc]
            npcs = [primary_npc] + eligible_others[:max(0, _yell_limit - 1)]
            approved_yell_speakers = {_clean_npc_name(n) for n in npcs if n}
            if not is_ambient and primary_npc and _is_sapient_yell_speaker(primary_npc) and not _recent_direct_chat_marked:
                mark_recent_direct_chat(primary_npc, context=context, explicit_id=primary_id)
                _recent_direct_chat_marked = True
        else:
            npcs = [primary_npc]
            approved_yell_speakers = {_clean_npc_name(n) for n in npcs if n}

        for _approved_speaker in approved_yell_speakers:
            _alias_key = _yell_speaker_alias_key(_approved_speaker)
            if _alias_key:
                approved_yell_speaker_aliases[_alias_key] = _approved_speaker

        if mode != 'yell' and not is_ambient and primary_npc:
            mark_recent_direct_chat(primary_npc, context=context, explicit_id=primary_id)

        if not primary_id:
            _, _lc = resolve_live_context(name=primary_npc, explicit_id=primary_id)
            _lc = _lc or {}
            if _lc:
                primary_id = _lc.get("runtime_id") or _lc.get("storage_id") or _lc.get("id")

        # No passive pre-generation here.
        # Only NPCs who will actually answer should get a real profile, and that
        # happens synchronously after the lightweight fetch below.

        char_datas = {}
        threads = []
        def fetch_npc_thread(name, cid, delay):
            if delay > 0:
                time.sleep(delay)
            try:
                npc_context, local_cid = get_local_context_and_id(name)
                thread_cid = cid if cid else local_cid
                char_datas[name] = get_character_data(name, npc_context, char_id=thread_cid, skip_generate=True)
            except Exception as e:
                logging.error(f"Thread Error fetching {name}: {e}")

        delay_counter = 0
        for name in save_listeners:
            cid = primary_id if name == primary_npc else None
            npc_context, local_cid = get_local_context_and_id(name)
            effective_id = cid if cid else local_cid
            ctx_dict = _parse_context_dict(npc_context)
            _, live_ctx = resolve_live_context(name=name, context=ctx_dict, explicit_id=effective_id)
            live_ctx = live_ctx or {}
        
            # Collision-safe storage ID for delay-check
            storage_id = (
                ctx_dict.get("storage_id")
                or live_ctx.get("storage_id")
                or make_storage_id(name, ctx_dict.get("faction") or live_ctx.get("faction", ""))
            )

            existing_profile = character_gateway.read(name, storage_id, ACTIVE_CAMPAIGN)

            delay = 0
            if (not existing_profile) or profile_needs_upgrade(existing_profile):
                delay = delay_counter
                delay_counter += 1
            
            t = threading.Thread(target=fetch_npc_thread, args=(name, cid, delay), daemon=True)
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join()

        # First contact generation: actual responders and the speaking player
        # get a full profile before persistence. The player is not added to the
        # responder set, but we still want a real entity for their own dialogue history.
        profiles_to_ensure = list(npcs)
        if player_name_clean and player_name_clean in save_listeners and player_name_clean not in profiles_to_ensure:
            profiles_to_ensure.append(player_name_clean)

        for responder_name in profiles_to_ensure:
            responder_profile = char_datas.get(responder_name)
            if responder_profile and not profile_needs_upgrade(responder_profile):
                continue
            responder_context, responder_local_cid = get_local_context_and_id(responder_name)
            responder_cid = primary_id if responder_name == primary_npc else responder_local_cid
            try:
                char_datas[responder_name] = get_character_data(
                    responder_name,
                    responder_context,
                    char_id=responder_cid,
                    skip_generate=False,
                )
                if profile_needs_upgrade(char_datas[responder_name]):
                    responder_sid = str(
                        char_datas[responder_name].get("ID")
                        or responder_cid
                        or responder_name
                    ).strip()
                    retry_deadline = time.time() + 2.0
                    while time.time() < retry_deadline:
                        with PROGRESS_LOCK:
                            in_progress = responder_sid in PROFILES_IN_PROGRESS
                        refreshed = character_gateway.reload(
                            responder_name,
                            responder_sid or None,
                            ACTIVE_CAMPAIGN,
                        )
                        if refreshed and not profile_needs_upgrade(refreshed):
                            char_datas[responder_name] = refreshed
                            logging.info(f"FIRST-CONTACT: refreshed ready profile for '{responder_name}' after pending handoff.")
                            break
                        if not in_progress:
                            break
                        time.sleep(0.1)
                who = "player" if responder_name == player_name_clean else "responder"
                logging.info(f"FIRST-CONTACT: generated profile for {who} '{responder_name}' before prompt/persist.")
            except Exception as responder_err:
                logging.error(f"FIRST-CONTACT: failed to generate profile for {responder_name}: {responder_err}")

        # Safety Fallback
        for name in npcs:
            if name not in char_datas or not char_datas[name]:
                logging.error(f"Failed to retrieve data for {name}, using pending fallback.")
                char_datas[name] = _build_pending_profile(name, name)
    
        # TALK mode now allows fall-through to prompt only the primary NPC
        # while others overheard via history updates above.

        logging.info(f"Prompting LLM for {mode} communication with {primary_npc} (Total participants: {len(npcs)})...")
        # Context building similar to Fallout 2 mod...
        primary_data = char_datas[primary_npc]

        # YELL action side-gate (Python-side safety layer):
        # If target_npc is on player's side, only player's-side NPC action tags are accepted.
        # If target_npc is outside player's side, only outside-side NPC action tags are accepted.
        _target_faction = str(
            primary_data.get("Faction")
            or primary_data.get("OriginFaction")
            or ""
        ).strip()
        if not _target_faction:
            try:
                _, _live_primary_ctx = resolve_live_context(
                    name=primary_npc,
                    context=primary_data,
                    explicit_id=primary_data.get("ID"),
                )
                _live_primary_ctx = _live_primary_ctx or {}
                _target_faction = str(
                    _live_primary_ctx.get("faction")
                    or _live_primary_ctx.get("Faction")
                    or _live_primary_ctx.get("origin_faction")
                    or ""
                ).strip()
            except Exception:
                _target_faction = ""

        # Non-sapient category routing
        persona_category = get_persona_category(
            primary_data.get("Race", "Unknown"),
            _target_faction,
            name=primary_npc,
            source="chat",
        )

        _yell_target_is_player_side = _is_player_side_faction(_target_faction)

        def _yell_speaker_allowed(speaker_name: str) -> bool:
            if mode != 'yell':
                return True

            s_clean = _resolve_yell_speaker_name(speaker_name)
            if not s_clean:
                return False

            if approved_yell_speakers and s_clean not in approved_yell_speakers:
                logging.info(f"YELL FILTER: blocked action from '{s_clean}' (speaker not in approved yell speaker set)")
                return False

            if not _is_sapient_yell_speaker(s_clean):
                logging.info(f"YELL FILTER: blocked action from '{s_clean}' (non-sapient speaker)")
                return False

            # Explicit player line (if ever leaked) is always treated as player-side.
            if s_clean == player_name_clean:
                speaker_is_player_side = True
            else:
                s_data = char_datas.get(s_clean) or char_datas.get(speaker_name) or {}
                s_faction = str(
                    s_data.get("Faction")
                    or s_data.get("OriginFaction")
                    or ""
                ).strip()

                if not s_faction:
                    try:
                        _, _live_s_ctx = resolve_live_context(
                            name=s_clean,
                            context=s_data if isinstance(s_data, dict) else None,
                            explicit_id=s_data.get("ID") if isinstance(s_data, dict) else None,
                        )
                        _live_s_ctx = _live_s_ctx or {}
                        s_faction = str(
                            _live_s_ctx.get("faction")
                            or _live_s_ctx.get("Faction")
                            or _live_s_ctx.get("origin_faction")
                            or ""
                        ).strip()
                    except Exception:
                        s_faction = ""

                # Conservative fallback: if unresolved and not primary, block command tags.
                if not s_faction and s_clean != _clean_npc_name(primary_npc):
                    logging.info(
                        f"YELL FILTER: blocked action from '{s_clean}' (unresolved faction; target side={_yell_target_is_player_side})"
                    )
                    return False

                if not s_faction and s_clean == _clean_npc_name(primary_npc):
                    s_faction = _target_faction

                speaker_is_player_side = _is_player_side_faction(s_faction)

            allowed = (speaker_is_player_side == _yell_target_is_player_side)
            if not allowed:
                side = "player-side" if speaker_is_player_side else "outside-side"
                target_side = "player-side" if _yell_target_is_player_side else "outside-side"
                logging.info(
                    f"YELL FILTER: blocked action from '{s_clean}' ({side}) because target is {target_side}"
                )
            return allowed
    
        # Simple history append for now — keep as list so the overflow guard can trim it
        def _sanitize_history_line(line):
            # Keep action/task tags visible to the LLM so it can learn from prior
            # successful game-state changes. Only strip server-side judgment tags.
            cleaned = re.sub(
                r'\[\s*JUDGMENT(?:\s*:\s*[^\]]+)?\s*\]',
                '',
                str(line or ""),
                flags=re.IGNORECASE
            )
            cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
            return cleaned

        _native_prompt_history_lines = max(1, int(load_settings().get("native_prompt_history_lines", 100)))
        history_lines = [
            _sanitize_history_line(line)
            for line in primary_data["ConversationHistory"][-_native_prompt_history_lines:]
            if _sanitize_history_line(line)
        ]
        history_str = "\n".join(history_lines)

        npc_profiles = ""
        for name in npcs:
            d = char_datas[name]
            if name != primary_npc:
                # Compact profile for secondary NPCs in all modes — saves ~80 tokens vs full format
                # Includes full personality + backstory opening sentence to preserve LLM accuracy
                faction = d.get('Faction', 'Unknown')
                race = d.get('Race', 'Unknown')
                job = d.get('Job', 'traveler')
                personality = d.get('Personality', 'A wanderer in the wasteland.')
                backstory = d.get('Backstory', '')
                backstory_note = (backstory.split('.')[0] + '.') if backstory else ''
                npc_profiles += f"\nCHARACTER: {name} ({race}, {faction}, {job}): {personality} {backstory_note}\n"
                continue

            # Full profile for the primary respondent (or any NPC in non-yell modes)
            npc_profiles += f"\nCHARACTER: {name}\n"
            npc_profiles += f"RACE: {d.get('Race')}\n"
            # Only pay for faction description once when origin and current match
            origin = d.get('OriginFaction', 'Unknown')
            current = d.get('Faction', 'Unknown')
            if origin == current or origin in ('Unknown', None):
                npc_profiles += f"FACTION: {get_faction_info(current)}\n"
            else:
                npc_profiles += f"ORIGIN FACTION: {get_faction_info(origin)}\n"
                npc_profiles += f"CURRENT FACTION: {get_faction_info(current)}\n"
            npc_profiles += f"JOB: {d.get('Job', 'None')}\n"
            npc_profiles += f"PERSONALITY: {d.get('Personality')}\n"
            npc_profiles += f"BACKSTORY: {d.get('Backstory')}\n"
            npc_profiles += f"PERSONAL RELATION TO PLAYER: {d.get('Relation', 0)} (Scale: -100 to 100)\n"
            _traits = d.get('Traits') or {}
            if _traits:
                _trait_parts = get_trait_parts(_traits)
                if _trait_parts:
                    npc_profiles += f"TRAITS: {' | '.join(_trait_parts)}\n"

            # Add live context (stats, health, etc.)
            _, live_ctx = resolve_live_context(name=name, context=d, explicit_id=d.get("ID"))
            live_context = build_detailed_context_string(name, char_data=d, live_ctx=live_ctx)
            if live_context:
                npc_profiles += f"{live_context}\n"

        primary_race = primary_data.get('Race', 'Unknown')
        is_animal = (persona_category == "animal")
        is_machine = (persona_category == "machine")
        is_feral = (persona_category == "feral")

        if is_machine:
            dynamic_system_prompt = f"CRITICAL: {primary_npc} is a MECHANICAL UNIT ({primary_race}). It is a robot or automated machine. It cannot speak human language or emote like an organic creature. It responds only with terse mechanical emissions, motion, or machine-status phrasing."
            final_instruction = f"Respond as {primary_npc} (the machine). Give one brief mechanical emission or machine-status line such as Servo whine. or Low confirmation chirr. Do not use brackets, full sentences, or human-style speech. Keep it under 6 words."
        elif is_feral:
            dynamic_system_prompt = f"CRITICAL: {primary_npc} is a FERAL being ({primary_race}). It does not speak human language. It reacts through hostile, fearful, territorial, or pack-instinct sounds and body language."
            final_instruction = f"Respond as {primary_npc} (the feral). Give one brief feral reaction in asterisks such as *Hisses and bares teeth.* or *Clicks sharply and lunges.* No human speech. Keep it under 6 words."
        elif is_animal:
            dynamic_system_prompt = f"CRITICAL: {primary_npc} is an ANIMAL ({primary_race}). Animals in Kenshi CANNOT speak human languages. They do not use words, symbols, or telegram-style speech. They ONLY react with brief physical actions, sounds, or gestures described within asterisks."
            final_instruction = f"Respond as {primary_npc} (the animal). Provide a single, BRIEF action description or sound in asterisks (e.g. *Growls*, *Tilts head*, *Nuzzles hand*). DO NOT USE WORDS OR SPEECH. Keep it under 6 words."
        else:
            dynamic_system_prompt = build_system_prompt(player_name, primary_data)
        
            if mode == 'yell':
                volume_status = "The player is addressing everyone nearby at a clear, projected volume."
                yell_instruction = f"\nCRITICAL: {volume_status} This can be heard by everyone nearby ({', '.join(npcs)}). This is a public address or talking to a crowd; it is NOT yelling or shouting aggressively. DO NOT tell the player to quiet down or react with annoyance to the volume. You SHOULD respond as multiple characters from the list to create a realistic crowd reaction. Every speaker MUST be on a new line started with 'Name: ' (e.g., 'Beep: Hey!').\nACTION TAGS IN CROWD MODE: If a character decides to take an action (attack, flee, join, etc.), place the [ACTION: TAG] at the END of THAT CHARACTER'S OWN LINE, not at the end of the whole response. Example: 'Hobbs: I'm with you! [ACTION: JOIN_PARTY]'\nCRITICAL JOIN RULE: If any character says they will follow, join, come along, or is 'in' (e.g. 'Count me in', 'I'll come', 'Lead the way', 'I'm with you'), that character's line MUST end with [ACTION: JOIN_PARTY]. Saying it in words WITHOUT the tag has NO game effect."
                dynamic_system_prompt += yell_instruction
            elif mode == 'whisper':
                volume_status = "The player is WHISPERING to you privately. This is a quiet, intimate, or secretive moment."
                whisper_instruction = f"\nCRITICAL: {volume_status} ONLY {primary_npc} should respond. Keep the tone hushed and private."
                dynamic_system_prompt += whisper_instruction
            else:
                volume_status = "The player is speaking at a normal, conversational volume."

                # Transition reinforcement: inform LLM they stopped the public address
                if "[ACTION: ADDRESSES GROUP]" in history_str:
                     volume_status += " They have STOPPED addressing the group and are now speaking at a calm, normal volume."
                 
                talk_instruction = f"\nINFO: {volume_status} Respond naturally. This is a standard, polite conversation. You are calm and composed. DO NOT tell the player to quiet down, do NOT react with annoyance to their volume, and do NOT mention noise or shouting unless they are actually being aggressive."
            
                dynamic_system_prompt += talk_instruction
                # added by Pineaxe v07 - move JUDGMENT to final_instruction so it lands
                # at the end of [FINAL INSTRUCTION] where the LLM reads it last.
                # Previously it was in dynamic_system_prompt but got buried under Kayak content.
                # JUDGMENT moved below — appended after final_instruction is fully built
        
            # If the player is talking to multiple people (Yell or Group Talk), adjust the instructions
            if len(npcs) > 1 and mode == 'yell':
                group_instruction = f"\nCONTEXT: You are facilitating a group conversation. YOU SHOULD RESPOND AS SEVERAL DIFFERENT CHARACTERS to create a lively atmosphere. Each speaker MUST use the format: 'Name: Dialogue'."
                dynamic_system_prompt += group_instruction

            final_instruction = f"Respond as {primary_npc} to the player's last message."
            if mode != 'yell':
                final_instruction = f"Respond ONLY as {primary_npc}. Do not speak as anyone else. Keep the response to 1-2 short sentences in a single paragraph."
            else:
                final_instruction = f"Respond as several characters from this list: ({', '.join(npcs)}) to the player's group address. Ensure at least 2-3 unique characters speak on separate lines if they are nearby."

        # Limit response length to discourage rambling
        final_instruction += " Keep it immersive, short, and grounded in the world of Kenshi. Response should be 1-3 sentences maximum."
        # added by Pineaxe v07 - remind LLM to use action tags at point of response.
        # Action tags defined in [SYSTEM CORE] are far from the response — LLMs often
        # ignore them. A brief reminder here dramatically improves compliance.
        final_instruction += " If the situation calls for a game action (giving an item, taking payment, attacking, following, etc.), append the appropriate [ACTION: TAG] at the very end of your response."
    
        # added by AntiGravity - prevent non-Sheks from hallucinating Shek slurs
        _speaker_race = str(primary_data.get("Race", "Unknown")).lower()
        if "shek" not in _speaker_race:
            final_instruction += " NEVER use the term 'flatskin' or 'flat skin'. That is a Shek-exclusive derogatory term."

        # added by Pineaxe v07 - JUDGMENT appended here so it's always last, after
        # final_instruction is fully built (avoids the order bug of appending before set).
        if not is_ambient and mode == 'talk':
            final_instruction += "\nJUDGMENT: At the end of your response, you MUST judge the player's tone on a scale of -5 (hostile) to 5 (friendly). 0 is neutral. Format as [JUDGMENT: n] at the very end."
            # The contract above described only the tag, and models sometimes
            # answered with the tag and nothing else. Everything but the tag is
            # then stripped, the line collapses to "..." and the NPC looks mute
            # for no in-fiction reason. Spell out that spoken words are the answer.
            final_instruction += "\nThe judgment tag is never the answer by itself. Always speak first: your reply MUST contain at least a few words your character actually says out loud, and only then the tag. Silence is not an option - a curt, reluctant or dismissive line is still a line."
    
        template = load_prompt_component("prompt_chat_template.txt", """[SYSTEM CORE]
    {system_prompt}

    [CURRENT CHARACTER: {primary_npc}]
    {npc_profiles}

    [CONVERSATION HISTORY]
    {history_str}

    [FINAL INSTRUCTION]
    {final_instruction}
    You MUST write your final response exclusively in {language_str}.
    """)
    
        settings = load_settings()
        user_lang = settings.get("language", "English")
        _knowledge_filters_enabled = bool(settings.get("enable_kayak_knowledge_filters", True))

        events_str = build_events_block()
        cdir = get_campaign_dir()
        _chron_env = (get_effective_player_context() or {}).get("environment", {})
        chronicle_str = build_chronicle_block(
            primary_data, cdir,
            current_location=(_chron_env.get("town_name", "")
                              if isinstance(_chron_env, dict) else ""),
        )
        # added by Pineaxe v04 - Kayak builds the prompt when available
        # Lazy reconnect: if Kayak was down at startup, try once per 60s
        global _KAYAK_LAST_RETRY
        import time as _time
        if not KAYAK_ENABLED and (_time.monotonic() - _KAYAK_LAST_RETRY > 60.0):
            logging.info("KAYAK: Attempting reconnect...")
            _kayak_try_connect()
        _kayak_used_this_request = False
        # added by Pineaxe v04 - /k_speak: build a speak prompt instead of normal chat prompt
        _kayak_vlow_avail = KAYAK_ENABLED
        if kayak_hub and getattr(kayak_hub, "_circuit_broken", False):
            _kayak_vlow_avail = False

        if _k_speak_mode:
            # Build speak prompt through Kayak only. Native fallback is disabled so tests
            # reveal missing mandatory prompt/token coverage immediately.
            _speak_prompt = ""
            if _kayak_vlow_avail:
                try:
                    _speak_prompt = kayak.build_speak_prompt(_k_speak_line, primary_npc, campaign=ACTIVE_CAMPAIGN)
                except Exception as _sp_err:
                    logging.error(f"KAYAK: build_speak_prompt failed ({_sp_err}); native fallback disabled.")
            if not _speak_prompt:
                return jsonify({"text": "[KAYAK PROMPT ERROR] Speak prompt unavailable and native fallback is disabled.", "actions": []}), 500
            rich_prompt = _speak_prompt
            _kayak_used_this_request = True
            logging.info(f"KAYAK: Speak prompt ready for {primary_npc}")
        if _kayak_vlow_avail and not is_ambient and not _k_speak_mode:
            try:
                _, _live_ctx_for_kayak = resolve_live_context(
                    name=primary_npc, context=primary_data,
                    explicit_id=primary_data.get("ID")
                )
                _kayak_shop_stock = []
                if _live_ctx_for_kayak:
                    # added by Pineaxe v04 - pass SHOP_STOCK so bridge can inject SHOP INVENTORY block
                    # SHOP_STOCK is written by the Kenshi C++ mod, read by SS at campaign load.
                    # Plain dict: NPC name -> list of item name strings.
                    _kayak_shop_stock = get_shop_stock_for_npc(primary_npc)
                    kayak.write_stats_from_context(
                        primary_npc, _live_ctx_for_kayak,
                        campaign=ACTIVE_CAMPAIGN,
                        shop_stock=_kayak_shop_stock,
                    )
                # Build knowledge filter: exclude distant entities for localized NPCs
                # Holy Nation farmer shouldn't know about Far-Away Cities, but can know their capitol
                _knowledge_filter = []
                if _knowledge_filters_enabled and _live_ctx_for_kayak and primary_npc:
                    _npc_faction = _live_ctx_for_kayak.get('faction', 'Unknown').lower()
                    _npc_location = (_live_ctx_for_kayak.get('environment') or {}).get('town_name', '').lower()
                
                    # Faction-specific knowledge filtering
                    # This tells Kayak to prioritize certain entities for retrieval
                    if 'holy' in _npc_faction or 'okran' in _npc_faction:
                        # Holy Nation: emphasize religious/faction entities
                        _knowledge_filter = [
                            "Holy Nation", "Okran", "Blister Hill", "Stack", "Rebirth",
                            "Holy Lands", "Okran's Fist", "religion", "faith", "scripture"
                        ]
                    elif 'united' in _npc_faction or 'city' in _npc_faction:
                        # United Cities: emphasize trade/commerce entities
                        _knowledge_filter = [
                            "United Cities", "Heng", "Heft", "trade", "commerce", "merchant",
                            "guild", "slavery", "profit", "business", "coin"
                        ]
                    elif 'shek' in _npc_faction:
                        # Shek Kingdom: emphasize combat/military entities
                        _knowledge_filter = [
                            "Shek Kingdom", "Admag", "Squin", "warrior", "combat", "honor",
                            "strength", "battle", "skeleton", "genocide"
                        ]
                
                    # Location-specific filters (supplement faction knowledge)
                    if _npc_location:
                        # NPCs in small towns have narrower knowledge
                        small_towns = ["outpost", "camp", "settlement", "town"]
                        is_small_town = any(t in _npc_location for t in small_towns)
                    
                        if is_small_town:
                            # Add local location and nearby references
                            _knowledge_filter.extend([
                                _npc_location, "local", "nearby", "merchants", "bandits",
                                "trade", "roads"
                            ])
                        else:
                            # Major cities → broader knowledge
                            _knowledge_filter.extend([
                                _npc_location, "trade routes", "politics", "news", "rumors"
                            ])
                
                    # Always include global/universal references
                    _knowledge_filter.extend(["Kenshi", "crossers", "world", "nomads"])
                
                    logging.debug(f"KAYAK: Knowledge filter for {primary_npc} ({_npc_faction}/{_npc_location}): {_knowledge_filter[:5]}... (+{len(_knowledge_filter)-5} more)")
            
                _ss_extra = "\n\n".join(filter(None, [chronicle_str])).strip()  # added by Pineaxe v07 - events_str excluded from chat: raw log only for rumour synthesiser
                _mode_guidance = ""
                if 'talk_instruction' in locals() and talk_instruction:
                    _mode_guidance = talk_instruction.strip()
                elif 'yell_instruction' in locals() and yell_instruction:
                    _mode_guidance = yell_instruction.strip()
                elif 'whisper_instruction' in locals() and whisper_instruction:
                    _mode_guidance = whisper_instruction.strip()
                _kayak_env = PLAYER_CONTEXT.get("environment", {}) if isinstance(PLAYER_CONTEXT, dict) else {}
                if not isinstance(_kayak_env, dict):
                    _kayak_env = {}
                _runtime_blocks = {
                    "player_name": player_name,
                    "player_context": dict(_eff_player),
                    "nearby": nearby if isinstance(nearby, list) else [],
                    "target_shop_stock": _kayak_shop_stock,
                    "campaign_chronicle": chronicle_str,
                    "recent_events": list(EVENT_HISTORY[-100:]),
                    "server_town": str(_kayak_env.get("town_name") or _kayak_env.get("town") or ""),
                    "server_region": str(_kayak_env.get("biome") or _kayak_env.get("region") or ""),
                    "player_status": format_player_status(_eff_player),
                    "player_inventory": format_player_inventory(_eff_player),
                    "mode_guidance": _mode_guidance,
                    "final_instruction": final_instruction,
                    "language_instruction": f"You MUST write your final response exclusively in {user_lang}.",
                    "world_synthesis_path": os.path.join(KENSHI_SERVER_DIR, "logs", "world_synthesis.log"),
                }
                rich_prompt = kayak.build_chat_prompt(
                    player_message = player_message,
                    target_npc     = primary_npc,
                    target_npc_id  = primary_id,
                    race           = primary_race,
                    persona_category = persona_category,
                    mode           = mode,
                    extra_context  = _ss_extra or None,
                    campaign       = ACTIVE_CAMPAIGN,
                    knower_context = _live_ctx_for_kayak if isinstance(_live_ctx_for_kayak, dict) else None,
                    knowledge_filters_enabled = _knowledge_filters_enabled,
                    knowledge_filter = _knowledge_filter if _knowledge_filter else None,
                    runtime_blocks = _runtime_blocks,
                )
                if rich_prompt:
                    _kayak_used_this_request = True
                    logging.info(f"KAYAK: Prompt built for {primary_npc} (mode={mode})")
                else:
                    logging.error("KAYAK: build_chat_prompt returned empty; native fallback disabled for token prompt testing.")
            except Exception as _kayak_prompt_err:
                logging.error(f"KAYAK: Prompt build failed ({_kayak_prompt_err}); native fallback disabled for token prompt testing.")
        if not _kayak_used_this_request:
            return jsonify({"text": "[KAYAK PROMPT ERROR] Kayak prompt unavailable and native fallback is disabled for token prompt testing.", "actions": []}), 500

        # --- Pre-flight context guard ---
        # LM Studio context: 16384 tokens. 15000 cap leaves headroom for output + buffer.
        # Kenshi prompts run ~3.5 chars/token (structured text, brackets, short words).
        # Use chars * 2 // 7 (≈ ÷ 3.5) to estimate tokens conservatively.
        _settings = load_settings()
        _CONTEXT_LIMIT   = int(_settings.get("prompt_context_limit", 11000))
        # added by Pineaxe v07 - configurable LLM parameters via settings.json
        # Fallback to hardcoded defaults if keys are absent (safe for existing installs).
        _MAX_DIAL_TOKENS     = int(_settings.get("chat_max_tokens",      500))
        _narrative_temp      = float(_settings.get("narrative_temperature", 0.8))
        _USER_MSG_BUFFER = int(_settings.get("prompt_user_message_buffer", 250))
        _PROMPT_BUDGET   = _CONTEXT_LIMIT - _MAX_DIAL_TOKENS - _USER_MSG_BUFFER  # 14600

        def _est_tokens(text):
            return len(text) * 2 // 7  # ≈ chars ÷ 3.5

        while (not _kayak_used_this_request) and _est_tokens(rich_prompt) > _PROMPT_BUDGET and history_lines:
            history_lines.pop(0)  # Drop oldest history line
            history_str = "\n".join(history_lines)
            rich_prompt = template.format(
                system_prompt=dynamic_system_prompt,
                primary_npc=primary_npc,
                npc_profiles=npc_profiles,
                chronicle_str=chronicle_str,
                events_str="",  # added by Pineaxe v07 - raw log excluded from chat prompt
                player_status_str=format_player_status(_eff_player),
                player_inventory_str=format_player_inventory(_eff_player),
                history_str=history_str,
                final_instruction=final_instruction,
                language_str=user_lang
            )

        est_tokens = _est_tokens(rich_prompt)
        logging.info(f"PROMPT: {primary_npc} | ~{est_tokens} tokens | {len(history_lines)} history lines")

        # Цикл выше ужимает только родную сборку. Промпт от Kayak собран в
        # одну строку, поэтому просим пересобрать его с меньшим числом реплик
        # истории: это самая крупная и самая выбрасываемая часть.
        if est_tokens > _PROMPT_BUDGET and _kayak_used_this_request:
            for _cap in (12, 6, 2, 0):
                try:
                    _shrunk = kayak.build_chat_prompt(
                        player_message = player_message,
                        target_npc     = primary_npc,
                        target_npc_id  = primary_id,
                        race           = primary_race,
                        persona_category = persona_category,
                        mode           = mode,
                        extra_context  = _ss_extra or None,
                        campaign       = ACTIVE_CAMPAIGN,
                        knower_context = _live_ctx_for_kayak if isinstance(_live_ctx_for_kayak, dict) else None,
                        knowledge_filters_enabled = _knowledge_filters_enabled,
                        knowledge_filter = _knowledge_filter if _knowledge_filter else None,
                        runtime_blocks = dict(_runtime_blocks, dialogue_lines_cap=_cap),
                    )
                except Exception as _shrink_err:
                    logging.warning(f"PROMPT: shrink to {_cap} history lines failed ({_shrink_err})")
                    break
                if not _shrunk:
                    break
                rich_prompt = _shrunk
                est_tokens = _est_tokens(rich_prompt)
                logging.info(
                    f"PROMPT: {primary_npc} trimmed to {_cap} history lines "
                    f"— ~{est_tokens} tokens (budget {_PROMPT_BUDGET})"
                )
                if est_tokens <= _PROMPT_BUDGET:
                    break

        if est_tokens > _PROMPT_BUDGET:
            logging.warning(f"PROMPT: Budget exceeded even with empty history (~{est_tokens} tokens). "
                            f"NPC profile too large for {_CONTEXT_LIMIT}-token context budget.")
            DEBUG_LOG = os.path.join(KENSHI_SERVER_DIR, "logs", "llm_debug.log")
            try:
                with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*50}\n")
                    f.write(f"TIMESTAMP: {time.ctime()}\n")
                    f.write(f"REQUEST FOR: {primary_npc} [GUARD FIRED - PROMPT TOO LARGE]\n")
                    f.write(f"EST. TOKENS: ~{est_tokens} | BUDGET: {_PROMPT_BUDGET}\n")
                    f.write(f"{'='*50}\n")
            except: pass
            # Раньше здесь возвращалось «...» — неотличимо от немногословного
            # NPC. Игрок гадал, почему собеседник молчит, вместо того чтобы
            # прочитать причину и поднять лимит.
            return jsonify({
                "text": _service_notice(
                    user_lang,
                    ru=(f"[SentientSands] Промпт для «{primary_npc}» не влезает: "
                        f"~{est_tokens} токенов при лимите {_PROMPT_BUDGET}. "
                        f"История разговора уже вырезана целиком. Подними "
                        f"PromptContextLimit в config_master.txt."),
                    en=(f"[SentientSands] Prompt for '{primary_npc}' does not fit: "
                        f"~{est_tokens} tokens against a {_PROMPT_BUDGET} budget. "
                        f"Dialogue history is already gone. Raise "
                        f"PromptContextLimit in config_master.txt."),
                ),
                "actions": [],
            }), 200

        # Tag the player message with mode for history clarity
        mode_action = ""
        if mode == 'whisper':
            mode_action = f" [ACTION: WHISPERS TO {primary_npc}]"
        elif mode == 'yell':
            mode_action = " [ACTION: ADDRESSES GROUP]"
        else:
            # If they were addressing the group before, explicitly state they are talking normally now
            if "[ACTION: ADDRESSES GROUP]" in history_str:
                mode_action = " [ACTION: TALKS NORMALLY]"
        time_prefix = get_current_time_prefix()
        full_player_entry = f"{time_prefix}{player_name}{mode_action}: {player_message}"

        messages = [
            {"role": "system", "content": rich_prompt},
            {"role": "user", "content": full_player_entry}
        ]
        if _k_speak_mode:
            _prompt_log_type = "speak"
        elif persona_category in ("animal", "machine", "feral", "sapient") and persona_category != "sapient":
            _prompt_log_type = f"chat_species_{persona_category}"
        elif mode == "yell":
            _prompt_log_type = "chat_yell"
        elif mode == "whisper":
            _prompt_log_type = "chat_whisper"
        else:
            _prompt_log_type = "chat"
        log_prompt_snapshot(_prompt_log_type, messages=messages, metadata={"npc": primary_npc, "campaign": ACTIVE_CAMPAIGN, "mode": mode, "persona_category": persona_category, "kayak_prompt": _kayak_used_this_request, "history_lines": len(history_lines)})
        # Debug Logging: Log the full request
        DEBUG_LOG = os.path.join(KENSHI_SERVER_DIR, "logs", "llm_debug.log")
        try:
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*50}\n")
                f.write(f"TIMESTAMP: {time.ctime()}\n")
                f.write(f"REQUEST FOR: {primary_npc} (Mode: {mode})\n")
                f.write(f"EST. TOKENS: ~{_est_tokens(rich_prompt)}\n")
                f.write(f"PROMPT:\n{rich_prompt}\n")
                f.write(f"USER MESSAGE: {player_message}\n")
                f.write(f"{'-'*30}\n")
        except: pass

        logging.info(f"Calling main chat LLM...")
        content = call_llm(messages, max_tokens=_MAX_DIAL_TOKENS, temperature=_narrative_temp)

        # Ответ из одних служебных тегов — примерно каждый двадцать третий.
        # Теги срежутся, реплика схлопнется в «...», и NPC будет выглядеть
        # немым без причины внутри вымысла. Переспрашиваем один раз, прямо
        # назвав промах: модель видит собственный пустой ответ и обычно
        # исправляется. Отключается настройкой RetrySilentReply.
        if (content and not _spoken_text(content)
                and settings.get("retry_silent_reply", True)):
            logging.warning(
                f"LLM: {primary_npc} answered with tags only "
                f"({content.strip()[:60]!r}) — retrying once"
            )
            _retry_messages = list(messages) + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    "Your reply contained no spoken words - only tags. The game "
                    "strips tags, so the player saw silence. Answer again as the "
                    "same character: one or two sentences your character actually "
                    "says out loud, and only then the tags. A curt, reluctant or "
                    "dismissive line is still a line. "
                    f"Write it exclusively in {user_lang}."
                )},
            ]
            _retry = call_llm(_retry_messages, max_tokens=_MAX_DIAL_TOKENS,
                              temperature=min(1.0, _narrative_temp + 0.2))
            if _retry and _spoken_text(_retry):
                # Действие первой попытки не теряем: она могла молча освободить
                # пленника и не сказать ни слова.
                if not _GAME_TAG_RE.search(_retry):
                    _carried = " ".join(_GAME_TAG_RE.findall(content))
                    if _carried:
                        _retry = f"{_retry.rstrip()} {_carried}"
                content = _retry
                logging.info(f"LLM: {primary_npc} spoke up on the retry")
            else:
                logging.warning(
                    f"LLM: {primary_npc} stayed silent on the retry too — "
                    f"falling back to '...'"
                )

        # Debug Logging: Log the response
        if content:
            try:
                with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                    f.write(f"RAW LLM RESPONSE:\n{content}\n")
                    f.write(f"{'='*50}\n")
            except: pass
        else:
            logging.error("LLM returned None for chat response.")
            try:
                with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                    f.write(f"LLM RESPONSE FAILED (None)\n")
                    f.write(f"{'='*50}\n")
            except: pass
    
        if content:
            # 0a. Normalize "Name: Speaker\nDialogue" → "Speaker: Dialogue" for yell mode.
            # Some models emit a header line instead of the inline "Speaker: text" format.
            if mode == 'yell':
                normalized_lines = []
                pending_name = None
                for raw_line in content.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.lower().startswith("name:"):
                        remainder = line[5:].strip()
                        if not remainder:
                            pending_name = None
                            continue
                        if ":" in remainder:
                            normalized_lines.append(remainder)
                            pending_name = None
                        else:
                            pending_name = remainder
                        continue
                    if pending_name:
                        normalized_lines.append(f"{pending_name}: {line}")
                        pending_name = None
                    else:
                        normalized_lines.append(line)
                content = "\n".join(normalized_lines)

            # 0. Per-speaker action parsing (for YELL/Group mode)
            # We must do this BEFORE global cleaning removes the tags.
            per_speaker_actions = []
            speaker_judgments = {} # speaker -> val
            if mode == 'yell':
                raw_lines = content.split('\n')
                for rline in raw_lines:
                    rline = rline.strip()
                    if not rline: continue
                    if rline.lower().startswith("name:"):
                        rline = rline[5:].strip()
                        if not rline:
                            continue
                    # Look for "Name: ... [TAG]"
                    match = re.match(r'^([^:]+):\s*(.*)$', rline)
                    if match:
                        speaker = match.group(1).strip()
                        speaker_canonical = _resolve_yell_speaker_name(speaker)
                        payload = match.group(2).strip()
                        # Extract ALL tags from this specific sub-line
                        speaker_tags = re.findall(r'\[\s*[^\]]+\s*\]', payload)
                        _has_join = any("JOIN_PARTY" in t.upper() for t in speaker_tags)
                        for stag in speaker_tags:
                            if not _yell_speaker_allowed(speaker_canonical):
                                continue
                            # Re-attribute: "Name: [TAG]"
                            per_speaker_actions.append(f"{speaker_canonical}: {stag}")
                            logging.info(f"YELL ATTRIBUTION: {speaker_canonical} took action {stag}")

                            # Extract judgment if present
                            if "JUDGMENT" in stag.upper():
                                j_match = re.search(r'-?\d+', stag)
                                if j_match:
                                    try:
                                        val = int(j_match.group(0))
                                        speaker_judgments[speaker_canonical] = max(-5, min(5, val))
                                    except: pass

                        # Yell recruitment safeguard: inject JOIN_PARTY if speaker agreed without tag
                        if not _has_join:
                            _yell_affirm = [
                                "count me in", "i'm with you", "i'm in", "lead the way",
                                "right behind you", "i'll follow", "i'll come", "let's go",
                                "stand with you", "i'll join", "by your side"
                            ]
                            _yell_refusals = [
                                r"\bno\b", r"\bnope\b", r"\bwon't\b", r"\bcan't\b",
                                r"\brefuse\b", r"\bnever\b", r"\bdecline\b"
                            ]
                            _pm_lower_y = player_message.lower()
                            _pay_lower = payload.lower()
                            _is_recruit_y = any(k in _pm_lower_y for k in ["join", "recruit", "follow me", "come with", "squad", "crew"])
                            _affirmed_y = any(p in _pay_lower for p in _yell_affirm)
                            _refused_y = any(re.search(p, _pay_lower) for p in _yell_refusals)
                            if _is_recruit_y and _affirmed_y and not _refused_y:
                                if _yell_speaker_allowed(speaker_canonical):
                                    per_speaker_actions.append(f"{speaker_canonical}: [ACTION: JOIN_PARTY]")
                                    logging.info(f"RECRUIT: Injected JOIN_PARTY for yell speaker {speaker_canonical} — prose agreement detected")

            # Use a very generous regex to find anything that looks like a tag
            all_bracketed = re.findall(r'\[\s*[^\]]+\s*\]', content)
        
            actions = []
            global_judgment = 0
        
            # Mapping of common sloppy keywords to formal C++ tags
            formal_map = {
                "GIVE_CATS": "GIVE_CATS", "TAKE_CATS": "TAKE_CATS", 
                "GIVE_ITEM": "GIVE_ITEM", "TAKE_ITEM": "TAKE_ITEM",
                "DROP_ITEM": "DROP_ITEM", "SPAWN_ITEM": "SPAWN_ITEM",
                "JOIN_PARTY": "JOIN_PARTY", "LEAVE": "LEAVE",
                "FOLLOW_PLAYER": "FOLLOW_PLAYER",
                "IDLE": "IDLE", "PATROL_TOWN": "PATROL_TOWN", 
                "RELEASE_PLAYER": "RELEASE_PLAYER", "FREE_PLAYER": "FREE_PLAYER",
                "NOTIFY": "NOTIFY", "FACTION_RELATIONS": "FACTION_RELATIONS",
                "ATTACK_TOWN": "ATTACK_TOWN", "TRAVEL_TO_TARGET_TOWN": "TRAVEL_TO_TARGET_TOWN",
                "RAID_TOWN": "RAID_TOWN", "ATTACK": "ATTACK",
                "RELEASE_PRISONER": "RELEASE_PRISONER", 
                "BREAKOUT_PRISONER": "BREAKOUT_PRISONER", "BREAKOUT_PLAYER": "BREAKOUT_PLAYER",
                "JOB_MEDIC": "JOB_MEDIC", "JOB_REPAIR_ROBOT": "JOB_REPAIR_ROBOT",
                "FIND_AND_RESCUE": "FIND_AND_RESCUE", "JUDGMENT": "JUDGMENT"
            }

            for raw in all_bracketed:
                inner = raw.strip("[] \t")
                # 1. Strip any recursive-like "ACTION:" or "TASK:" prefixes first
                # We use a loop to handle weird double-prefixes like "ACTION: ACTION: TAKE_CATS"
                clean = inner
                while True:
                    prev = clean
                    clean = re.sub(r'^(ACTION|TASK|TAG):\s*', '', clean, flags=re.IGNORECASE).strip()
                    if clean == prev: break
            
                # 2. Extract Keyword and Args
                if ":" in clean:
                    parts = clean.split(":", 1)
                    kw = parts[0].strip().upper()
                    args = parts[1].strip()
                
                    # Recursive keyword fix: Handle [ACTION: TAKE_CATS: TAKE_CATS: TAKE_CATS: 40]
                    while args.upper().startswith(kw):
                        args = re.sub(rf'^{re.escape(kw)}\s*:?\s*', '', args, flags=re.IGNORECASE).strip()
                else:
                    kw = clean.upper()
                    args = ""

                # 3. Handle Judgment (Extract value for server logic only — never forward to DLL)
                if kw == "JUDGMENT" or "JUDGMENT" in kw:
                    j_val = args or re.search(r'-?\d+', kw)
                    if j_val:
                        try:
                            j_str = j_val.group(0) if hasattr(j_val, 'group') else str(j_val)
                            global_judgment = max(-5, min(5, int(j_str)))
                            logging.info(f"RELATION: Interaction judged as {global_judgment}")
                        except: pass
                    continue  # JUDGMENT is server-side only; sending it to the DLL breaks bubble display

                # 4. Handle Actions/Tasks
                # Fuzzy match the keyword against our known list
                matched_ka = None
                for formal in formal_map:
                    if formal == kw or (formal in kw and len(kw) < len(formal) + 3):
                        matched_ka = formal_map[formal]
                        break
            
                if matched_ka:
                    # Strip quantity suffixes from GIVE_ITEM/TAKE_ITEM args (e.g. "Skeleton Leg x4" -> "Skeleton Leg")
                    if matched_ka in ("GIVE_ITEM", "TAKE_ITEM", "DROP_ITEM"):
                        args = re.sub(r'\s*[x×]\s*\d+\s*$', '', args, flags=re.IGNORECASE).strip()

                    # Normalize item names to canonical Kenshi template IDs
                    if matched_ka in ("GIVE_ITEM", "TAKE_ITEM", "DROP_ITEM"):
                        args = _normalize_item_arg_preserve_count(args)
                    elif matched_ka == "SPAWN_ITEM" and args:
                        # SPAWN_ITEM format: "Template:Count | Name | Description"
                        # Normalize only the template while preserving optional :count.
                        args = _normalize_spawn_args(args)

                    # Rebuild the tag exactly as C++ expects it, avoiding double prefixes
                    if matched_ka in ["WANDERER", "CHASE", "IDLE", "MELEE_ATTACK"]:
                        final_tag = f"[TASK: {matched_ka}{f': {args}' if args else ''}]"
                    else:
                        # Special case: LEAVE needs the origin faction for squad dismissal
                        if matched_ka == "LEAVE" and not args:
                            origin_faction = primary_data.get("Faction", "Unknown")
                            final_tag = f"[ACTION: LEAVE: {origin_faction}]" if origin_faction != "Unknown" else "[ACTION: LEAVE]"
                        else:
                            final_tag = f"[ACTION: {matched_ka}{f': {args}' if args else ''}]"
                
                    # Check for redundant task assigned in consecutive turns
                    if "TASK:" in final_tag:
                         last_hist = primary_data["ConversationHistory"][-1] if primary_data["ConversationHistory"] else ""
                         if final_tag in last_hist: continue

                    if matched_ka in ("GIVE_ITEM", "SPAWN_ITEM", "GIVE_CATS", "TAKE_CATS") and persona_category in ("animal", "feral"):
                        logging.info(
                            f"ACTION BLOCK: blocked commerce action '{matched_ka}' for {primary_npc} ({persona_category})"
                        )
                        continue

                    # GIVE_ITEM inventory check: if item not in NPC's live inventory, fall back to SPAWN_ITEM.
                    # For shopkeepers, always use SPAWN_ITEM — ACT_GIVE_ITEM searches the NPC's personal
                    # inventory by substring match; shop items live in a separate container object and are
                    # never found that way, so GIVE_ITEM silently fails for traders.
                    if matched_ka == "GIVE_ITEM" and args:
                        _, _resolved_live_ctx = resolve_live_context(name=primary_npc, context=primary_data, explicit_id=primary_data.get("ID"))
                        _live_ctx_gi = _resolved_live_ctx or {}
                        _is_shopkeeper = bool(_live_ctx_gi.get("is_trader", False))
                        # Worn gear cannot change hands at all. ACT_GIVE_ITEM walks the
                        # NPC's inventory sections; equipped weapons and armour live outside
                        # them (the DLL reads those through getEquippedWeapons/getEquippedArmour
                        # only when it builds the prompt), so the transfer silently fails.
                        # Falling through to SPAWN_ITEM would be worse than failing: it mints
                        # a second copy on the ground and the NPC keeps the original.
                        _inv_gi = _live_ctx_gi.get("inventory", [])
                        _arg_l = args.lower()

                        def _names_match(_n):
                            return _arg_l in _n or _n in _arg_l

                        _loose_gi = [i.get("name", "").lower() for i in _inv_gi if not i.get("equipped") and i.get("name")]
                        _worn_gi = [i.get("name", "").lower() for i in _inv_gi if i.get("equipped") and i.get("name")]
                        if (not any(_names_match(n) for n in _loose_gi)
                                and any(_names_match(n) for n in _worn_gi)):
                            logging.info(
                                f"TRADE: '{args}' is equipped by {primary_npc} — the game cannot "
                                f"transfer worn gear, GIVE_ITEM dropped instead of duplicating it"
                            )
                            continue
                        if _is_shopkeeper:
                            _shop_stock = get_shop_stock_for_npc(primary_npc)
                            if _shop_stock is not None and not _shop_stock_matches_item(_shop_stock, args):
                                logging.info(
                                    f"TRADE: Shopkeeper '{primary_npc}' does not list '{args}' in cached shop stock - allowing SPAWN_ITEM fallback"
                                )
                            final_tag = f"[ACTION: SPAWN_ITEM: {args} | {args} | A trade item.]"
                            logging.info(
                                f"TRADE: Shopkeeper '{primary_npc}' — switched GIVE_ITEM to SPAWN_ITEM for delivery"
                            )
                        else:
                            _live_inv = _live_ctx_gi.get("inventory", [])
                            _held_names = [i.get("name", "").lower() for i in _live_inv if not i.get("equipped")]
                            _item_lower = args.lower()
                            _in_inventory = any(_item_lower in n or n in _item_lower for n in _held_names)
                            if not _in_inventory:
                                # A plain villager is not a shop: falling back to SPAWN_ITEM
                                # here would conjure goods out of nothing every time an NPC
                                # offered something it does not actually carry. Drop the tag
                                # instead — the prompt tells ordinary NPCs to trade only what
                                # is in their inventory. We only dare drop it when we can see
                                # a non-empty inventory; an empty one usually means the live
                                # context did not resolve, and blocking on that would be worse
                                # than the old behaviour.
                                if _live_inv:
                                    logging.info(
                                        f"TRADE: '{args}' is not carried by {primary_npc} and they are "
                                        f"no trader — GIVE_ITEM dropped instead of conjuring a copy"
                                    )
                                    continue
                                final_tag = f"[ACTION: SPAWN_ITEM: {args} | {args} | A trade item.]"
                                logging.info(
                                    f"TRADE: '{args}' not in {primary_npc}'s inventory and no live "
                                    f"inventory to check against — kept SPAWN_ITEM fallback"
                                )

                    # SPAWN_ITEM создаёт новый объект, а не передаёт существующий.
                    # Для настоящего торговца это его товар и всё честно, но когда
                    # у NPC просят его собственное снаряжение, получается дубликат:
                    # игроку копия на землю, а оригинал остаётся на NPC. Модель шлёт
                    # такой тег напрямую, минуя проверку GIVE_ITEM выше, поэтому
                    # ловим отдельно.
                    if matched_ka == "SPAWN_ITEM" and args:
                        _tmpl_sp = re.sub(r":\s*\d+\s*$", "",
                                          args.split("|")[0].strip()).strip().lower()
                        if _tmpl_sp:
                            _, _spawn_ctx = resolve_live_context(name=primary_npc, context=primary_data, explicit_id=primary_data.get("ID"))
                            _spawn_ctx = _spawn_ctx or {}
                            _worn_sp = [i.get("name", "").lower()
                                        for i in _spawn_ctx.get("inventory", [])
                                        if i.get("equipped") and i.get("name")]
                            if any(_tmpl_sp in n or n in _tmpl_sp for n in _worn_sp):
                                # Оружейник вполне может носить то же, чем торгует,
                                # поэтому надетое прощаем только если вещь реально
                                # числится в товаре лавки.
                                _stock_sp = (get_shop_stock_for_npc(primary_npc)
                                             if _spawn_ctx.get("is_trader") else None)
                                if not (_stock_sp is not None
                                        and _shop_stock_matches_item(_stock_sp, _tmpl_sp)):
                                    logging.info(
                                        f"TRADE: '{_tmpl_sp}' is worn by {primary_npc} and is not "
                                        f"listed shop stock — SPAWN_ITEM dropped, it would have "
                                        f"duplicated their own gear"
                                    )
                                    continue

                    actions.append(final_tag)

            # YELL target-side gate for unattributed/global actions:
            # If an action is not speaker-attributed, we treat it as coming from the
            # primary target NPC. If that side is blocked for this yell, drop it.
            if mode == 'yell' and actions and (not _yell_speaker_allowed(primary_npc)):
                logging.info(
                    f"YELL FILTER: dropped {len(actions)} unattributed action(s) from primary '{primary_npc}' due to side gate"
                )
                actions = []

            relation_dirty_names = set()

            # 5. Apply judgment to NPC's personal relation score and faction relations
            if not is_ambient:
                # Aggregate all participants who judged
                judges = speaker_judgments if speaker_judgments else {primary_npc: global_judgment}
            
                for judge_name, j_val in judges.items():
                    if j_val == 0: continue
                
                    # Get data for this speaker (must have been loaded in char_datas)
                    j_data = char_datas.get(judge_name)
                    if not j_data: 
                        # If it's a yell participant we didn't fully load, skip
                        continue

                    current_rel = j_data.get("Relation", 0)
                    try: current_rel = int(current_rel)
                    except: current_rel = 0
                
                    new_rel = max(-100, min(100, current_rel + j_val))
                    if new_rel != current_rel:
                        j_data["Relation"] = new_rel
                        relation_dirty_names.add(judge_name)
                        logging.info(f"RELATION: {judge_name} personal relation updated {current_rel} -> {new_rel} (judgment={j_val})")

                    # Faction relation impact (Only for dramatic judgments).
                    # The prompt constrains JUDGMENT to -5..5, so thresholds must
                    # live inside that range or they will never fire.
                    f_delta = 0
                    if j_val >= 4:
                        f_delta = 1
                    elif j_val <= -4:
                        f_delta = -1
                
                    if f_delta != 0:
                        npc_f = j_data.get("Faction", "None")
                        if npc_f and npc_f not in ["None", "Nameless", "No Faction"]:
                            f_tag = f"[ACTION: FACTION_RELATIONS: {npc_f}: {f_delta}]"
                            actions.append(f_tag)
                            logging.info(f"RELATION: Scheduled faction relation change via {judge_name} for {npc_f}: {f_delta}")

            # 5b. Recruitment intent safeguard (direct talk only)
            # Detect when: player asked NPC to join + NPC agreed in prose + tag was omitted by model
            _join_tag = "[ACTION: JOIN_PARTY]"
            if _join_tag not in " ".join(actions) and mode != 'yell':
                _recruit_asks = [
                    "join", "recruit", "come with me", "travel with me", "follow me",
                    "part of my squad", "part of my group", "my crew", "my team"
                ]
                _affirm_phrases = [
                    "stand with you", "i'll follow", "follow you", "count me in",
                    "lead the way", "right behind you", "i'm in", "i'm with you",
                    "i'll come", "by your side", "i'll join", "signed on",
                    "let's go", "i'll stand"
                ]
                _refusal_patterns = [
                    r"\bno\b", r"\bnope\b", r"\bwon't\b", r"\bcan't\b", r"\bcannot\b",
                    r"\brefuse\b", r"\bnever\b", r"\bnot going\b", r"\bstay here\b",
                    r"\bdecline\b", r"\bnot interested\b"
                ]
                _pm_lower = player_message.lower()
                _ct_lower = content.lower()
                _is_recruit_ask = any(kw in _pm_lower for kw in _recruit_asks)
                _has_affirmation = any(p in _ct_lower for p in _affirm_phrases)
                _has_refusal = any(re.search(p, _ct_lower) for p in _refusal_patterns)
                if _is_recruit_ask and _has_affirmation and not _has_refusal:
                    actions.append(_join_tag)
                    logging.info(f"RECRUIT: Injected JOIN_PARTY for {primary_npc} — prose agreement detected without tag")

            # 5c. Trade payment safeguard (direct talk only)
            # Detect when: player explicitly paid/agreed + amount in message + TAKE_CATS was omitted
            if "[ACTION: TAKE_CATS" not in " ".join(actions) and mode != 'yell':
                _pay_confirms = [
                    "deal", "take the cats", "here are the cats", "here's the cats",
                    "here are your", "here is your", "here are my", "here is my",
                    "here you go", "i'll pay", "i'll take it", "take it", "agreed",
                    "take the money", "here's the money"
                ]
                _pm_lower_t = player_message.lower()
                _is_paying = any(k in _pm_lower_t for k in _pay_confirms)
                if _is_paying:
                    # First try to find the amount in the player's own message
                    _amt_match = re.search(r'\b(\d+)\s*cats?\b', _pm_lower_t)
                    if not _amt_match:
                        # Player confirmed without stating a number (e.g. "take the cats") —
                        # fall back to the price the NPC quoted in their current response
                        _amt_match = re.search(r'\b(\d+)\s*cats?\b', content.lower())
                    if _amt_match:
                        _pay_amount = int(_amt_match.group(1))
                        actions.append(f"[ACTION: TAKE_CATS:{_pay_amount}]")
                        logging.info(f"TRADE: Injected TAKE_CATS:{_pay_amount} for {primary_npc} — player confirmed payment without tag")

            # 5d. Trade conflict cleanup: money can flow only one direction per exchange.
            _has_give_cats = any("[ACTION: GIVE_CATS" in a for a in actions)
            _has_take_cats = any("[ACTION: TAKE_CATS" in a for a in actions)
            if _has_give_cats and _has_take_cats:
                _has_take_item = any("[ACTION: TAKE_ITEM" in a for a in actions)
                _has_give_item = any("[ACTION: GIVE_ITEM" in a or "[ACTION: SPAWN_ITEM" in a for a in actions)
                _content_lower_trade = content.lower()

                def _drop_money(kind):
                    kept = []
                    removed = 0
                    for _a in actions:
                        if kind == "GIVE_CATS" and "[ACTION: GIVE_CATS" in _a:
                            removed += 1
                            continue
                        if kind == "TAKE_CATS" and "[ACTION: TAKE_CATS" in _a:
                            removed += 1
                            continue
                        kept.append(_a)
                    return kept, removed

                _drop_kind = None
                if _has_take_item and not _has_give_item:
                    _drop_kind = "TAKE_CATS"
                elif _has_give_item and not _has_take_item:
                    _drop_kind = "GIVE_CATS"
                elif any(k in _content_lower_trade for k in ["your payment", "your pay", "for the iron", "for the ore", "for your work", "reward"]):
                    _drop_kind = "TAKE_CATS"
                elif any(k in _content_lower_trade for k in ["that'll be", "price is", "costs", "for sale", "you can have it for"]):
                    _drop_kind = "GIVE_CATS"
                else:
                    _drop_kind = "TAKE_CATS"

                actions, _removed = _drop_money(_drop_kind)
                logging.info(
                    f"TRADE: Removed {_removed} conflicting {_drop_kind} tag(s) for {primary_npc} "
                    f"because money cannot flow both directions in one trade"
                )

            # 5e. An NPC cannot pay what it does not have. The DLL credits the
            # player unconditionally on GIVE_CATS and only afterwards decides
            # whether to debit the NPC, so a tag larger than the NPC's purse
            # mints cats out of nothing. Clamp each tag to what the NPC really
            # carries, and drop it entirely when the purse is empty.
            if any("[ACTION: GIVE_CATS" in a for a in actions):
                _purse = None
                try:
                    _, _money_ctx = resolve_live_context(name=primary_npc)
                    if _money_ctx is not None:
                        _purse = int(str(_money_ctx.get("money")).strip())
                except (TypeError, ValueError, AttributeError):
                    _purse = None
                if _purse is not None and _purse >= 0:
                    _kept, _notes = [], []
                    for _a in actions:
                        _m = re.search(r"\[ACTION:\s*GIVE_CATS\s*:?\s*(\d+)", _a)
                        if not _m:
                            _kept.append(_a)
                            continue
                        _want = int(_m.group(1))
                        if _want <= _purse:
                            _kept.append(_a)
                            continue
                        if _purse == 0:
                            _notes.append(f"{_want}->dropped")
                            continue
                        _kept.append(f"[ACTION: GIVE_CATS:{_purse}]")
                        _notes.append(f"{_want}->{_purse}")
                    if _notes:
                        actions = _kept
                        logging.warning(
                            f"TRADE: {primary_npc} cannot afford GIVE_CATS "
                            f"({', '.join(_notes)}; purse {_purse}) — clamped to "
                            f"stop the game minting cats"
                        )

            # 5f. Money taken, nothing delivered. This is how a broken prompt or a
            # dropped tag actually hurts the player: the NPC pockets the cats and
            # the goods never appear. Worse, the bad turn is what gets written to
            # the NPC's history — history stores the final action list — so the
            # model reads it back next turn and repeats it word for word until
            # every purchase is a robbery. Always warn; cancel the payment in the
            # two cases where it is certainly wrong. A plain villager keeps its
            # TAKE_CATS: for them it is also how gifts, debts and bribes arrive.
            # What the player asked for decides everything here. A handover with
            # nothing demanded in return is a gift and must go through; a purchase
            # or a swap only earns the payment once the goods actually moved.
            _pm_intent = str(player_message or "").lower()
            _gift_words = (
                "подар", "дарю", "дарк", "просто так", "бесплатн", "за службу",
                "за помощь", "за работу", "награда", "награду", "премия", "премию",
                "чаевые", "угощаю", "от меня", "это тебе", "тебе за", "на выпивку",
                "не надо ничего", "ничего не надо", "ничего взамен", "без обмена",
                "gift", "for free", "no charge", "keep it", "reward",
            )
            _swap_words = (
                "куплю", "купить", "покупаю", "продай", "продаш", "продаёш",
                "продашь", "беру у теб", "сколько стоит", "почём", "почем",
                "за это дай", "взамен", "в обмен", "обменя", "меняю", "махнём",
                "buy", "sell", "trade me", "how much", "in exchange",
            )
            _is_gift = any(w in _pm_intent for w in _gift_words)
            _is_swap = any(w in _pm_intent for w in _swap_words)
            _gave_item = any(
                ("[ACTION: GIVE_ITEM" in a or "[ACTION: SPAWN_ITEM" in a)
                for a in actions
            )

            if any("[ACTION: TAKE_CATS" in a for a in actions) and not _gave_item:
                if _is_gift and not _is_swap:
                    logging.info(
                        f"TRADE: {primary_npc} accepts {_pm_intent[:40]!r} as a gift — "
                        f"TAKE_CATS kept, nothing is owed in return"
                    )
                    _drop_take = None
                else:
                    logging.warning(
                        f"TRADE: {primary_npc} took cats but delivered no item — "
                        f"actions were {actions}"
                    )
                    # Warning alone is not enough: this robs the player for
                    # real, and the bad turn then lands in the NPC's history —
                    # history stores the final action list — so the model reads
                    # it back and repeats it until every purchase is a robbery.
                    _drop_take = None
                    if content.rstrip().endswith("?"):
                        # Still asking ("Восемьдесят, берёшь?") — nothing was agreed.
                        _drop_take = "the deal is still a question, nothing was agreed"
                    elif _is_swap:
                        _drop_take = "the player was buying, not giving, and no goods moved"
                    else:
                        try:
                            _, _tk_ctx = resolve_live_context(name=primary_npc, context=primary_data, explicit_id=primary_data.get("ID"))
                        except Exception:
                            _tk_ctx = None
                        if (_tk_ctx or {}).get("is_trader"):
                            # A shopkeeper taking money always owes goods in return.
                            _drop_take = "a trader must hand over goods to be paid"
                    if _drop_take:
                        actions = [a for a in actions if "[ACTION: TAKE_CATS" not in a]
                        logging.warning(
                            f"TRADE: cancelled TAKE_CATS for {primary_npc} — {_drop_take}"
                        )

            # The same fairness rule for barter: if the player offered a swap and
            # the NPC pockets their item while handing over neither goods nor cats,
            # the swap did not happen. Robbery and plain gifts are untouched — in
            # neither case did the player propose an exchange.
            if (_is_swap and not _is_gift
                    and any("[ACTION: TAKE_ITEM" in a for a in actions)
                    and not _gave_item
                    and not any("[ACTION: GIVE_CATS" in a for a in actions)):
                actions = [a for a in actions if "[ACTION: TAKE_ITEM" not in a]
                logging.warning(
                    f"TRADE: cancelled TAKE_ITEM for {primary_npc} — the player offered "
                    f"a swap but nothing came back"
                )

            # 5h. Тот же заказ оплачен второй раз. Товар лавочника падает на
            # землю, игрок его не видит и переспрашивает — а строка истории
            # хранит теги действий, и модель повторяет весь прошлый блок
            # целиком: коты списываются снова, товар дублируется. Настоящий
            # повторный заказ игрок называет словами, по ним и отличаем.
            _repeat_words = (
                "ещё", "еще", "повтори", "снова", "опять", "добавь", "докупл",
                "another", "one more", "again", "repeat", "more",
            )
            _this_deal = _trade_signature(actions)
            if _this_deal and any(s.startswith("TAKE_CATS:") for s in _this_deal):
                _prev_line = _last_own_history_line(
                    (char_datas.get(primary_npc) or {}).get("ConversationHistory"),
                    primary_npc,
                )
                _wants_more = _is_swap or any(w in _pm_intent for w in _repeat_words)
                if _trade_signature(_prev_line) == _this_deal and not _wants_more:
                    actions = [a for a in actions if not _TRADE_TAG_RE.search(a)]
                    logging.warning(
                        f"TRADE: dropped a repeat of the previous deal by {primary_npc} — "
                        f"the player never ordered again ({', '.join(_this_deal)})"
                    )

            # 6. Clean Dialogue Text - strip backend action tags but preserve narrative
            # brackets like [laughter] or [sighs]. Anchored to known tag prefixes so
            # hallucinated variants like [ACTION: TAKE_CATS: TAKE_CATS: 40] are still caught.
            content = re.sub(
                r'\[\s*(?:ACTION|TASK|TAG|STATUS|EFFECT|EMOTE|THOUGHT)(?:\s*:\s*[^\]]+)?\s*\]',
                '', content, flags=re.IGNORECASE
            ).strip()

            # Strip fake inventory/money bracket notations the model sometimes invents
            # e.g. "[1300 cats removed from Houston's inventory]", "[JUDGMENT: 5]" already handled above
            content = re.sub(r'\[\s*\d[\d,]*\s*cats?\s+[^\]]{0,60}\]', '', content, flags=re.IGNORECASE).strip()
            content = re.sub(r'\[\s*\d[\d,]*\s*(?:removed|added|transferred|deducted)[^\]]{0,60}\]', '', content, flags=re.IGNORECASE).strip()

            # In YELL mode, prefer per-speaker attributed actions so C++ resolves
            # each action to the correct NPC via the "NpcName: [ACTION: X]" prefix.
            # This also avoids double-firing from mixed attributed/unattributed tags.
            if mode == 'yell':
                if per_speaker_actions:
                    logging.info(f"YELL ACTIONS: {per_speaker_actions}")
                    actions = list(per_speaker_actions)

                # Stable de-dup in crowd mode.
                if actions:
                    _seen_actions = set()
                    _deduped = []
                    for _a in actions:
                        if _a in _seen_actions:
                            continue
                        _seen_actions.add(_a)
                        _deduped.append(_a)
                    actions = _deduped

            # Advanced Cleaning
            content = content.strip()
        
            # Split into lines and filter out thoughts/meta-text
            lines = content.split('\n')
            filtered_lines = []
            for line in lines:
                line = line.strip()
                if not line: continue

                # Filter lines that leak prompt context back into the response
                if re.search(r'\[(?:Visible Gear|SHOP STOCK|Inventory Held)', line, re.IGNORECASE):
                    continue

                # Re-apply tag removal to individual lines just in case
                line = re.sub(r'\[\s*[^\]]+\s*\]', '', line).strip()
                if not line: continue

                # If in YELL mode, look for "Name: Response" format to split bubbles
                is_group_response = (mode == 'yell')
                if is_group_response:
                    if line.lower().startswith("name:"):
                        line = line[5:].strip()
                        if not line:
                            continue
                    # Try to extract "Beep: Hello!" or "Hobbs: Let's go."
                    match = re.match(r'^([^:]+):\s*(.*)$', line)
                    if match:
                        actor_name = match.group(1).strip()
                        actor_clean_name = _resolve_yell_speaker_name(actor_name)
                        actor_clean = _yell_speaker_alias_key(actor_clean_name)
                        player_clean_key = _yell_speaker_alias_key(player_name_clean)
                        actor_speech = match.group(2).strip()
                        if approved_yell_speakers and actor_clean_name not in approved_yell_speakers:
                            logging.info(f"Hallucination Filter: Discarded yell line from unapproved speaker {actor_name}")
                            continue
                        # Only accept if actor is NOT the player (hallucination)
                        if actor_clean != player_clean_key:
                            # Use full ID if mapping exists to aid C++ resolution
                            full_actor = name_to_id.get(actor_clean_name, actor_clean_name)
                            filtered_lines.append(f"{full_actor}: {actor_speech}")
                            continue
                        else:
                            logging.info(f"Hallucination Filter: Discarded LLM attempt to speak as {player_name}")
                            continue
            
                # Skip common non-dialogue prefixes/meta-talk and hallucinated log lines
                lower_line = line.lower()
                _meta_prefixes = [
                    "thought:", "thinking:", "observation:", "note:", "(thinking", 
                    "as an ai", "i cannot", "here is", "raw llm response:", 
                    "timestamp:", "request for:", "prompt:", "user message:",
                    "history:", "character:", "personality:", "backstory:", "current condition"
                ]
                if any(lower_line.startswith(prefix) for prefix in _meta_prefixes):
                    continue
                if lower_line.startswith("*") and persona_category not in ("animal", "feral"):
                    continue
            
                # Skip separator lines
                if line.startswith('=') or line.startswith('-') or len(set(line)) <= 2:
                    continue
                
                # Remove "CHARACTER_NAME: " prefixes ONLY if NOT in multi/squad mode
                if len(npcs) <= 1:
                    # Hallucination Filter: If talking to ONE person, ensure they don't speak as the player or someone else
                    prefix_match = re.match(r'^([A-Za-z0-9 _\-\.]+):\s*', line)
                    if prefix_match:
                        p = prefix_match.group(1).strip().lower()
                        if p == player_name.lower():
                            logging.info(f"Hallucination Filter: Discarded player entry {line}")
                            continue
                        if p != primary_npc.lower():
                            # Discard line for a different persona
                            logging.info(f"Hallucination Filter: Discarded line from {p} (expected {primary_npc})")
                            continue
                    # Strip the prefix if it existed
                    line = re.sub(r'^[A-Za-z0-9 _\-\.]+:\s*', '', line)
                    # Discard lines that are solely an NPC name (e.g. "Benek\n" before the dialogue)
                    if line.lower() in [n.lower() for n in npcs]:
                        continue

                # intra-line splitting for multi/squad talk (catch "Name1: text Name2: text")
                if len(npcs) > 1:
                    # Find all "Name: Dialogue" blocks
                    # We look for a name followed by a colon, then text until the next name: or string end
                    # The name must avoid common dialogue words
                    pattern = r'([A-Z][A-Za-z0-9 _\-\.\'\"“”‘’`]+):\s*([^:]+?)(?=\s+[A-Z][A-Za-z0-9 _\-\.\'\"“”‘’`]+:\s*|$)'
                    sub_matches = re.findall(pattern, line)
                    if sub_matches:
                        for actor, speech in sub_matches:
                            actor_clean = actor.strip()
                            actor_key = _resolve_yell_speaker_name(actor_clean)
                            if approved_yell_speakers and actor_key not in approved_yell_speakers:
                                logging.info(f"Hallucination Filter: Discarded packed yell line from unapproved speaker {actor_clean}")
                                continue
                            if _yell_speaker_alias_key(actor_key) != _yell_speaker_alias_key(player_name_clean):
                                full_actor = name_to_id.get(actor_key, actor_key)
                                filtered_lines.append(f"{full_actor}: {speech.strip()}")
                        continue

                if line:
                    filtered_lines.append(line)
        
            # Join lines - newlines represent separate bubbles in multi-NPC mode
            if filtered_lines:
                if mode != 'yell':
                    # For single responder modes, merge into one bubble to prevent rapid-fire flashing
                    content = " ".join(filtered_lines)
                else:
                    content = "\n".join(filtered_lines)
            else:
                # Nothing survived filtering — usually the model answered with
                # the judgment tag alone. The NPC then looks mute for no reason
                # inside the fiction, so make it visible instead of swallowing it.
                content = "..."
                logging.warning(
                    f"LLM: empty reply for {primary_npc} after filtering — no "
                    f"spoken line was produced, falling back to '...'"
                )

            # Final safety truncation
            _MAX_SINGLE_RESPONSE_CHARS = 500
            _MAX_YELL_RESPONSE_CHARS = 900
            _MAX_YELL_LINES = 8
            if mode == 'yell':
                if len(content) > _MAX_YELL_RESPONSE_CHARS or len(filtered_lines) > _MAX_YELL_LINES:
                    kept_lines = []
                    total_chars = 0
                    for line in filtered_lines:
                        next_total = total_chars + (1 if kept_lines else 0) + len(line)
                        if kept_lines and (len(kept_lines) >= _MAX_YELL_LINES or next_total > _MAX_YELL_RESPONSE_CHARS):
                            break
                        if not kept_lines and len(line) > _MAX_YELL_RESPONSE_CHARS:
                            kept_lines.append(line[:_MAX_YELL_RESPONSE_CHARS - 3].rstrip() + "...")
                            break
                        kept_lines.append(line)
                        total_chars = next_total
                    content = "\n".join(kept_lines) if kept_lines else "..."
            elif len(content) > _MAX_SINGLE_RESPONSE_CHARS:
                content = content[:_MAX_SINGLE_RESPONSE_CHARS - 3] + "..."
        
            # Log initial player prompt to global history once
            player_faction = get_effective_player_context().get("faction", "None")
            primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
            record_event_to_history("CHAT", player_name, primary_npc, player_message, actor_faction=player_faction, target_faction=primary_faction)

            # Save history for ALL listeners (Participants + Overhearers)
            for name in save_listeners:
                is_player_profile = (_clean_npc_name(name) == player_name_clean)
                is_overhearing = (name not in npcs) and (not is_player_profile)
                overheard_tag = "(Overheard) " if is_overhearing else ""
            
                if name not in char_datas:
                    # Need to fetch for overhearers who weren't participants
                    ctx, sid = get_local_context_and_id(name)
                    char_datas[name] = get_character_data(name, ctx, char_id=sid, skip_generate=True)
                
                char_datas[name]["ConversationHistory"].append(f"{time_prefix}{overheard_tag}{player_name}{mode_action}: {player_message}")
            
                # If multiple lines/speakers, append them all to history
                if "\n" in content:
                    _content_lines = [l.strip() for l in content.split('\n') if l.strip()]
                    for _idx, line in enumerate(_content_lines):
                        # Ensure the line has a speaker attribution in the history
                        history_line = line
                        # Strip pipe IDs from speaker names (e.g. "Name|12345: text" → "Name: text")
                        if _has_speaker_prefix(history_line) and '|' in history_line.split(':', 1)[0]:
                            _spk, _rest = history_line.split(':', 1)
                            history_line = f"{_spk.split('|')[0].strip()}: {_rest.strip()}"
                        if not _has_speaker_prefix(history_line):
                            # Append primary name if LLM forgot the prefix in single-responder modes
                            history_line = f"{primary_npc}: {history_line}"

                        # If this is the LAST line and there are actions, append them for history context
                        if _idx == len(_content_lines) - 1 and actions:
                            history_line += f" {' '.join(actions)}"

                        char_datas[name]["ConversationHistory"].append(f"{time_prefix}{overheard_tag}{history_line}")

                        # Log NPC speech to global history
                        if _has_speaker_prefix(history_line):
                            h, m = history_line.split(':', 1)
                            speaker_name = h.strip()
                            speaker_faction = char_datas.get(speaker_name, {}).get("Faction", "None")
                            player_faction = get_effective_player_context().get("faction", "None")
                            record_event_to_history("CHAT", speaker_name, player_name, m.strip(), actor_faction=speaker_faction, target_faction=player_faction)
                        else:
                            primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
                            player_faction = get_effective_player_context().get("faction", "None")
                            record_event_to_history("CHAT", primary_npc, player_name, history_line, actor_faction=primary_faction, target_faction=player_faction)
                else:
                    # Fallback for single-line responses
                    history_line = content
                    # Strip pipe IDs from speaker names (e.g. "Name|12345: text" → "Name: text")
                    if _has_speaker_prefix(history_line) and '|' in history_line.split(':', 1)[0]:
                        _spk, _rest = history_line.split(':', 1)
                        history_line = f"{_spk.split('|')[0].strip()}: {_rest.strip()}"
                    if not _has_speaker_prefix(history_line):
                         history_line = f"{primary_npc}: {history_line}"
                
                    history_entry = f"{time_prefix}{overheard_tag}{history_line}"
                    if actions:
                        history_entry += f" {' '.join(actions)}"
                    char_datas[name]["ConversationHistory"].append(history_entry)
                
                    primary_faction = char_datas.get(primary_npc, {}).get("Faction", "None")
                    player_faction = get_effective_player_context().get("faction", "None")
                    record_event_to_history("CHAT", primary_npc, player_name, content, actor_faction=primary_faction, target_faction=player_faction)

                _persist_conversation_history_target(
                    name,
                    char_datas,
                    relation_dirty_names=relation_dirty_names,
                    persist_source="chat_persist",
                )

            # Kayak dialogue history is synced above via character_gateway.write_dialogue_only().
            # Only keep the location-memory growth here.
            if KAYAK_ENABLED and _kayak_used_this_request and not is_ambient:
                _chat_location = ""
                if _live_ctx_for_kayak:
                    _env = _live_ctx_for_kayak.get("environment") or {}
                    _chat_location = str(_env.get("town_name") or _env.get("town") or "").strip()
                if _chat_location:
                    try:
                        kayak.grow_npc_knowledge(
                            target_npc=primary_npc,
                            location=_chat_location,
                            target_npc_id=primary_id,
                            campaign=ACTIVE_CAMPAIGN,
                        )
                    except Exception as _kayak_knowledge_err:
                        logging.warning(f"KAYAK: grow_npc_knowledge failed for {primary_npc}: {_kayak_knowledge_err}")
                    _overhearers = [n for n in listeners if n not in npcs and n != primary_npc]
                    for _oh_name in _overhearers:
                        try:
                            _oh_profile = char_datas.get(_oh_name) or {}
                            _oh_id = _oh_profile.get("ID") or None
                            kayak.grow_npc_knowledge(
                                target_npc=_oh_name,
                                location=_chat_location,
                                target_npc_id=_oh_id,
                                campaign=ACTIVE_CAMPAIGN,
                            )
                        except Exception as _oh_err:
                            logging.warning(f"KAYAK: grow_npc_knowledge failed for {_oh_name}: {_oh_err}")
            logging.info(f"AI RESPONSE: {content} | ACTIONS: {actions}")

            # Queue primary NPC for batch upgrade if their profile is still transient.
            # Nearby NPCs are handled by the pre-chat batch loop; the primary NPC is not.
            # Placed after release() so the queue is not deferred by the direct-chat active guard.
            _primary_profile = char_datas.get(primary_npc)
            _primary_sid = _primary_profile.get("ID", primary_npc) if _primary_profile else primary_npc
            _needs_upgrade = profile_needs_upgrade(_primary_profile) if _primary_profile else False
            _diag_running = False
            if _needs_upgrade:
                with PROGRESS_LOCK:
                    _diag_running = _primary_sid in PROFILES_IN_PROGRESS
            if not _primary_profile or _needs_upgrade:
                logging.info(
                    f"UPGRADE_DIAG: npc={primary_npc!r} "
                    f"present={bool(_primary_profile)} "
                    f"transient={_primary_profile.get('_transient') if _primary_profile else 'N/A'} "
                    f"needs={_needs_upgrade} "
                    f"sid={_primary_sid!r} "
                    + (f"in_progress={_diag_running}" if _needs_upgrade else "")
                )
            if _primary_profile and _needs_upgrade:
                _psid = _primary_sid
                _already_running = _diag_running
                if not _already_running:
                    queue_batch_profile_generation([{
                        "name": primary_npc,
                        "storage_id": _psid,
                        "race": _primary_profile.get("Race") or primary_ctx_dict.get("race", "Unknown"),
                        "gender": _primary_profile.get("Sex") or primary_ctx_dict.get("gender", "Unknown"),
                        "faction": _primary_profile.get("Faction") or primary_ctx_dict.get("faction", "Unknown"),
                        "origin_faction": _primary_profile.get("OriginFaction") or primary_ctx_dict.get("origin_faction", "Unknown"),
                        "job": _primary_profile.get("Job") or primary_ctx_dict.get("job", "None"),
                        "runtime_id": primary_ctx_dict.get("runtime_id") or primary_ctx_dict.get("id"),
                        "persistent_id": primary_ctx_dict.get("persistent_id"),
                    }])
                    logging.info(f"UPGRADE: Queued transient primary NPC '{primary_npc}' ({_psid}) for batch profile upgrade.")

            return jsonify({"text": content, "actions": actions})
        pass  # Released by context manager
        # Сюда попадаем, когда call_llm вернул None: провайдер не ответил или
        # упёрся в лимит. Раньше это тоже выглядело как «...», и отличить сбой
        # связи от неразговорчивого NPC было невозможно.
        return jsonify({
            "text": _service_notice(
                user_lang,
                ru=("[SentientSands] Модель не ответила. Проверь связь и лимиты "
                    "провайдера — подробности в server/logs/error_report.log."),
                en=("[SentientSands] The model did not answer. Check the provider "
                    "connection and rate limits — details in "
                    "server/logs/error_report.log."),
            ),
            "actions": [],
        })


def record_event_to_history(etype, actor, target, msg, actor_faction="None", target_faction="None"):
    """Centralized helper to record events for both the log and narrative synthesis."""
    global EVENT_HISTORY, EVENT_HISTORY_SET, GLOBAL_EVENT_COUNTER, EVENT_THROTTLE, LAST_STATE_LOG
    if not msg: return

    # Snapshot once so the entire event line uses a consistent player context,
    # even if another /context update lands while we are formatting it.
    ctx = get_effective_player_context()

    # Format: [TYPE] Actor (Faction) -> Target (Faction) @ Location: Message
    p_fact = ctx.get('faction', 'Nameless')
    a_fact_display = actor_faction
    if actor_faction == "Nameless" or actor_faction == p_fact:
        a_fact_display = f"Player's Squad: {p_fact}"

    t_fact_display = target_faction
    if target_faction == "Nameless" or target_faction == p_fact:
        t_fact_display = f"Player's Squad: {p_fact}"

    actor_part = f"{actor} ({a_fact_display})" if a_fact_display and a_fact_display != "None" else actor
    target_part = f"{target} ({t_fact_display})" if t_fact_display and t_fact_display != "None" else target

    # Include location from player context if available
    location = ""
    if ctx:
        env = ctx.get("environment", {})
        town = env.get("town_name", "") if isinstance(env, dict) else ""
        if town:
            location = f" @ {town}"

    time_str = ""
    if ctx:
        day = ctx.get('day', 0)
        hour = int(ctx.get('hour', 0))
        minute = int(ctx.get('minute', 0))
        time_str = f"[Day {day}, {hour:02d}:{minute:02d}]"
    prefix = f"{time_str} " if time_str else ""
    evt_str = f"{prefix}[{etype}] {actor_part} -> {target_part}{location}: {msg}"

    # --- STATE SUPPRESSION ---
    # For repetitive state hooks (knockout, recovery, etc), only log if the status actually CHANGES.
    state_key = f"{target_part}|{etype}"
    with STATE_LOCK:
        if LAST_STATE_LOG.get(state_key) == msg:
            return  # Message is identical to last recorded state, skip
        LAST_STATE_LOG[state_key] = msg
        # Cleanup if it gets massive
        if len(LAST_STATE_LOG) > 2000: LAST_STATE_LOG.clear()

    # --- THROTTLE CHECK ---
    # Cooldown for non-stateful rapid repeats
    throttle_key = f"{etype}|{actor_part}|{target_part}|{msg}"
    now = time.time()
    with THROTTLE_LOCK:
        last_time = EVENT_THROTTLE.get(throttle_key, 0)
        # Increased cooldown to 30s for exact same event to prevent spam
        if now - last_time < 30.0:
            return
        EVENT_THROTTLE[throttle_key] = now
        # Periodic cleanup: Instead of clearing everything, just trim if it gets too large
        if len(EVENT_THROTTLE) > 1000:
            # Simple way to trim: keep most recent half
            sorted_items = sorted(EVENT_THROTTLE.items(), key=lambda x: x[1])
            EVENT_THROTTLE = dict(sorted_items[500:])

    # Log to file (Active Campaign Log) - Always log for live debugger feed
    try:
        cdir = get_campaign_dir()
        log_dir = os.path.join(cdir, "logs")
        if not os.path.exists(log_dir): os.makedirs(log_dir)
        with open(os.path.join(log_dir, "global_events.log"), "a", encoding="utf-8") as f:
            f.write(f"{evt_str}\n")
            f.flush()
            
        # Also copy to server.log so it shows up in both tabs if relevant
        logging.info(f"EVENT: {evt_str}")
    except Exception as e:
        logging.warning(f"EVENT: Failed to write to global_events.log: {e}")

    # Simple deduplication based on exact string for memory/synthesis
    # CRITICAL: Filter out "looting" events from the narrative history to prevent spam.
    if etype == "looting":
        return

    if evt_str not in EVENT_HISTORY_SET:
        # Летопись: сюда попадают только смерти, тюрьма и смена владельца
        # города. Бои запоминаются отдельно, чтобы у смерти был виновник.
        try:
            _env = ctx.get("environment", {}) if ctx else {}
            consider_event(
                get_campaign_dir(), etype, actor, target, msg,
                actor_faction=a_fact_display,
                target_faction=t_fact_display,
                location=(_env.get("town_name", "") if isinstance(_env, dict) else ""),
                region=(_env.get("biome", "") if isinstance(_env, dict) else ""),
                day=(ctx.get("day") if ctx else None),
            )
        except Exception as _chron_err:
            logging.warning(f"CHRONICLE: hook failed: {_chron_err}")

        EVENT_HISTORY.append(evt_str)
        EVENT_HISTORY_SET.add(evt_str)
        GLOBAL_EVENT_COUNTER += 1
        if GLOBAL_EVENT_COUNTER % 10 == 0:
            save_campaign_history()
        if len(EVENT_HISTORY) > 500:
            del EVENT_HISTORY[:-500]
            EVENT_HISTORY_SET = set(EVENT_HISTORY)

def _queue_auto_synthesis():
    """Mark an automatic world synthesis request for later foreground execution."""
    global AUTO_SYNTHESIS_PENDING, AUTO_SYNTHESIS_PENDING_AT
    AUTO_SYNTHESIS_PENDING = True
    AUTO_SYNTHESIS_PENDING_AT = time.time()


def _drain_auto_synthesis_if_pending():
    """Run queued automatic synthesis from a foreground request thread."""
    global AUTO_SYNTHESIS_PENDING, AUTO_SYNTHESIS_PENDING_AT
    if not AUTO_SYNTHESIS_PENDING:
        return None
    AUTO_SYNTHESIS_PENDING = False
    queued_at = AUTO_SYNTHESIS_PENDING_AT
    AUTO_SYNTHESIS_PENDING_AT = 0.0
    try:
        age = max(0.0, time.time() - float(queued_at or 0.0))
        logging.info(f"NARRATIVE: Draining queued automatic synthesis on foreground request (age={age:.1f}s).")
        return generate_global_narrative_thread(notify_player=False)
    except Exception as e:
        logging.error(f"NARRATIVE: Queued automatic synthesis failed: {e}")
        return None


def generate_global_narrative_thread(notify_player=True):
    """Synthesizes the last 100 events into a global rumor for NPCs to overhear."""
    global EVENT_HISTORY
    # Lower threshold for manual trigger so small sessions can still synthesize
    min_needed = 5
    if len(EVENT_HISTORY) < min_needed:
        logging.warning(f"NARRATIVE: Not enough events to synthesize (have {len(EVENT_HISTORY)}, need {min_needed}).")
        return None
    
    settings = load_settings()

    # Cap at 75 events to keep synthesis prompt under ~2K tokens (was 250, caused ~10K token prompts).
    sample_size = min(len(EVENT_HISTORY), 75)
    last_chunk = EVENT_HISTORY[-sample_size:]
    events_text = ""

    # Pre-compress raw events into compact daily summaries before feeding to LLM.
    # Turns ~75 verbose lines into ~10-15 summary lines, covering more days of history
    # in the same token budget → richer, more varied rumours.
    if _HAVE_COMPRESSOR:
        try:
            raw_text = "\n".join(last_chunk)
            events = _sge_parse(raw_text)
            compressed = _sge_reduce(_sge_compress(events))
            if compressed.strip():
                events_text = compressed if compressed.endswith("\n") else (compressed + "\n")
                summary_line_count = len([line for line in events_text.splitlines() if line.strip()])
                logging.info(f"NARRATIVE: Compressed {sample_size} events → {summary_line_count} summary lines.")
            else:
                logging.warning("NARRATIVE: Compressor returned empty output — falling back to raw events.")
        except Exception as _ce:
            logging.error(f"NARRATIVE: Compressor failed ({_ce}) — falling back to raw events.")

    if not events_text:
        # GROUP BY LOCATION
        # Events often contain " @ TownName"
        grouped_events = {}
        for evt in last_chunk:
            location = "Unknown Region"
            if " @ " in evt:
                # Extract location between " @ " and the following ":"
                try:
                    parts = evt.split(" @ ")
                    if len(parts) > 1:
                        location = parts[1].split(":")[0].strip()
                except Exception as e:
                    logging.warning(f"NARRATIVE: Failed to parse location from event line ({e})")

            if location not in grouped_events:
                grouped_events[location] = []
            grouped_events[location].append(evt)

        # Format grouped text
        for loc, evts in grouped_events.items():
            events_text += f"\n--- {loc.upper()} ---\n"
            events_text += "\n".join(evts) + "\n"

        logging.info(f"NARRATIVE: Grouped {len(last_chunk)} events into {len(grouped_events)} locations.")
    else:
        logging.info("NARRATIVE: Using compressor-generated grouped summaries directly.")
    # logging.debug(f"NARRATIVE GROUPING:\n{events_text}")
    
    # Load existing rumors to prevent repeats
    past_rumors_block = ""
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
    if os.path.exists(world_events_path):
        try:
            with open(world_events_path, "r", encoding="utf-8") as f:
                # Find the actual [RUMOR: ...] text in the last few lines
                rumor_lines = []
                for line in f.readlines()[-20:]: # Scan last 20 lines
                    match = re.search(r'\[RUMOR:\s*(.*?)\]', line)
                    if match:
                        rumor_lines.append(f"- {match.group(1).strip()}")
                
                if rumor_lines:
                    past_rumors_block = "\nPREVIOUS RUMORS (Do NOT repeat these):\n" + "\n".join(rumor_lines[-5:])
        except Exception as e:
            logging.warning(f"NARRATIVE: Failed to read past rumors from world_events.txt ({e})")

    p_fact = get_effective_player_context().get("faction", "The Nameless")
    
    template = load_prompt_component("prompt_world_synthesis.txt", """[KENSHI WORLD SYNERGY]
The following is a log of recent interactions in the world of Kenshi, grouped by location.
Your task is to synthesize these events into a single, high-impact 'Global Rumor'.

RECENT LOGS:
{events_text}
{past_rumors_block}

INSTRUCTIONS:
1. Treat the PLAYER and their squad ({p_fact}) as just another group of wanderers. 
2. DO NOT make the player out to be a hero or legend unless they have performed a truly massive feat (e.g. liberating a city or killing a faction leader).
3. ONLY attribute events to the player if their name or 'Player's Squad' actually appears as an actor in the logs.
4. If an actor is 'Unknown', do NOT assume it is the player. Treat it as a mysterious figure or a random incident.
5. If the player is merely starving, dying, or performing minor trades, either ignore it or mention it as a minor misfortune of another 'unlucky nomad'.
6. Focus on patterns: frequent battles, faction clashes, or specific NPC actions.
7. Write one flavorful, cynical rumor — 1 to 3 sentences. Ground it in Kenshi's brutal reality.
8. Output ONLY the rumor text itself, with no prefix tags or formatting.
9. DO NOT blow minor scuffles out of proportion; keep it grounded.
10. VARIETY: Do NOT produce a rumor that is logically identical to the PREVIOUS RUMORS listed above.
""")
    prompt = template.format(events_text=events_text, past_rumors_block=past_rumors_block, p_fact=p_fact)

    # added by Pineaxe v04 - use Kayak loremaster prompt if available
    if KAYAK_ENABLED:
        try:
            _kl = kayak.build_loremaster_prompt(events_text, campaign=ACTIVE_CAMPAIGN)
            if _kl:
                prompt = _kl
                logging.info("KAYAK: Using Kayak loremaster prompt for narrative synthesis")
        except Exception as _kle:
            logging.warning(f"KAYAK: build_loremaster_prompt failed ({_kle}) - using native prompt")

    # Язык — последним. Промпт летописца из Kayak заменяет собой весь текст,
    # и раньше указание языка терялось именно здесь.
    language = settings.get("language", "English")
    if language and language.lower() != "english":
        prompt += f"\nLANGUAGE: You MUST write the rumor ONLY in {language}. Do not use English."
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Synthesize one grounded Kenshi rumor from these recent events."}
    ]
    
    log_prompt_snapshot("world_synthesis", messages=messages, metadata={"campaign": ACTIVE_CAMPAIGN, "events_chars": len(events_text), "kayak_prompt": KAYAK_ENABLED})
    log_prompt_snapshot("global_events", messages=messages, metadata={"campaign": ACTIVE_CAMPAIGN, "events_chars": len(events_text), "kayak_prompt": KAYAK_ENABLED})
    logging.info("NARRATIVE: Calling LLM to synthesize world events...")
    rumor_text = call_llm(messages, max_tokens=int(load_settings().get("narrative_max_tokens", 150)), temperature=float(load_settings().get("narrative_temperature", 0.8)))  # added by Pineaxe v07 - configurable via settings.json
    
    if rumor_text:
        # Strip any accidental tags the LLM might still output
        rumor_text = rumor_text.strip()
        # If LLM still used the old format, extract just the inner text
        tag_match = re.search(r'\[RUMOR:\s*(.*?)\]', rumor_text, re.DOTALL)
        if tag_match:
            rumor_text = tag_match.group(1).strip()
        # Remove any leading dashes or bullets
        rumor_text = re.sub(r'^[-•*]\s*', '', rumor_text).strip()
        
        if len(rumor_text) > 10:
            time_prefix = get_current_time_prefix().strip()
            rumor_tagged = f"- {time_prefix} [RUMOR: {rumor_text}]"
            # Try campaign dir first
            world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
            if not os.path.exists(world_events_path):
                # Create empty if missing
                with open(world_events_path, "w", encoding="utf-8") as f:
                    f.write("# Dynamic rumors generated for this campaign\n")

            try:
                with open(world_events_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{rumor_tagged}\n")
                logging.info(f"NARRATIVE: Generated and saved new global event: {rumor_tagged}")
                # added by Pineaxe v04 - write rumor to Kayak as retrievable world_event entity
                if KAYAK_ENABLED:
                    try:
                        import re as _re_slug
                        _rslug = _re_slug.sub(r'[^\w]', '_', time_prefix.strip())[:40].strip('_')
                        kayak.write_world_event(
                            name     = f"rumor_{_rslug}",
                            summary  = rumor_text,
                            campaign = ACTIVE_CAMPAIGN,
                        )
                    except Exception as _kwe:
                        logging.warning(f"KAYAK: write_world_event failed: {_kwe}")
                # Notify player using a short fixed phrase when safe.
                # Long dynamic rumor text appears unsafe for in-game NOTIFY delivery during
                # automatic synthesis, but a tiny static message can still act as a UI refresh signal.
                if notify_player:
                    send_to_pipe(f"NOTIFY: [WORLD EVENT] {rumor_text}")
                else:
                    send_to_pipe("NOTIFY: A new rumor is spreading.")
                    logging.info("NARRATIVE: Sent fixed auto-synthesis NOTIFY to refresh rumor UI.")
                return rumor_tagged
            except Exception as e:
                logging.error(f"Error saving global event rumor: {e}")
    return None

@app.route('/synthesize', methods=['POST'])
def manual_synthesize():
    """Manual trigger for global narrative synthesis."""
    # Run synchronously for the manual trigger so we can return the result
    rumor = generate_global_narrative_thread()
    if rumor:
        return jsonify({"status": "ok", "rumor": rumor})
    else:
        return jsonify({"status": "error", "message": "Failed to generate rumor or not enough events (need 5)."}), 400


@app.route('/events', methods=['GET', 'POST'])
def list_events():
    logging.info(f"ROUTE: /events [{request.method}]")

    """Return only synthesized [RUMOR:] entries from world_events.txt.
    Left list: '1. First few words...' — no # symbol (avoids MyGUI color-tag parsing).
    Right panel: full formatted card for the selected rumor.
    """
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
    rumors = []

    if os.path.exists(world_events_path):
        try:
            with open(world_events_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            rumor_count = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Find [RUMOR: ...] anywhere in the line to skip over new date tags
                match = re.search(r'\[RUMOR:\s*(.*?)\]', stripped)
                if not match:
                    continue

                rumor_count += 1
                inner = match.group(1).strip()

                # Build a safe label: "N. first 7 words..." with no special chars
                words = inner.split()
                short = " ".join(words[:7]) + ("..." if len(words) > 7 else "")
                label = f"{rumor_count}. {short}"
                rumors.append({"id": str(i + 1), "title": label[:80], "content": stripped, "inner": inner})

        except Exception as e:
            logging.error(f"Error reading world_events.txt: {e}")

    formatted = "--- DYNAMIC WORLD RUMORS ---\n" + "\n".join(r["content"] for r in rumors) if rumors else "(No rumors yet. Use 'Synthesize Rumors' to generate some.)"
    return jsonify({"status": "ok", "text": formatted, "events": rumors})


@app.route('/events/content', methods=['POST'])
def events_content():
    """Return formatted multi-line detail text for a selected world event entry.
    The right panel (SetEventsText) splits on newlines, so each line becomes a row.
    """
    data = request.json or {}
    line_id = data.get("day", "")
    
    # Only use campaign-specific events
    world_events_path = os.path.join(get_campaign_dir(), "world_events.txt")
        
    try:
        line_num = int(line_id) - 1  # id is 1-indexed line number
        with open(world_events_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if 0 <= line_num < len(lines):
            raw = lines[line_num].strip()
            match = re.search(r'\[RUMOR:\s*(.*?)\]', raw)
            if match:
                # Extract plain text from the capture group
                inner = match.group(1).strip()
                wrapped = textwrap.wrap(inner, width=76)
                card_lines = [
                    "=" * 38,
                    "  WORLD RUMOR",
                    "=" * 38,
                    "",
                ] + wrapped + [
                    "",
                    "(Synthesized from recent world events)"
                ]
                return jsonify({"status": "ok", "text": "\n".join(card_lines)})
    except Exception as e:
        logging.error(f"events/content error: {e}")
    return jsonify({"status": "error", "text": "Entry not found."}), 404

@app.route('/selected_character', methods=['POST'])
def update_selected_character():
    """Report the player character currently selected in-game.

    Posted on a timer by the SelectedSpeaker RE_Kenshi plugin. The body is a flat
    character record (name, ids, race, gender, faction, medical, stats, inventory).
    An empty body or {"clear": true} drops the selection and restores the stock
    squad-slot-1 speaker.

    Kept deliberately cheap and non-throwing — it runs every couple of seconds.
    """
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("clear"):
            data = {}
        data.pop("clear", None)
        stored = set_selected_player_context(data)
        if stored:
            debug_logger.debug(f"SELECTED: speaker is now {stored.get('name')!r}")
        else:
            debug_logger.debug("SELECTED: selection cleared")
        return jsonify({"status": "ok", "selected": bool(stored)})
    except Exception as e:
        logging.error(f"selected_character error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/context', methods=['POST'])
def update_context():
    # PLAYER_CONTEXT and LAST_NPC_NAME live in ss_identity (no global declaration needed)
    data = request.json
    if not data: return jsonify({"status": "error"}), 400

    # Process and deduplicate world events
    # SKIP processing if the game is paused or at speed 0 to prevent loops
    is_paused = data.get("is_paused", False)
    game_speed = data.get("gamespeed", 1.0)
    is_player_ctx = data.get("type") == "player"

    # Refresh PLAYER_CONTEXT before processing events so this payload's events
    # inherit the correct day, time, faction, and location. Mutate in place so
    # imported references in other modules stay live.
    if is_player_ctx:
        prev_paused = PLAYER_CONTEXT.get("is_paused")
        stale_keys = set(PLAYER_CONTEXT.keys()) - set(data.keys())
        PLAYER_CONTEXT.update(data)
        for key in stale_keys:
            PLAYER_CONTEXT.pop(key, None)
        if prev_paused != data.get("is_paused"):
             logging.info(f"CONTEXT: Player pause state changed to {data.get('is_paused')} (Speed: {data.get('gamespeed')})")

    if not is_paused and game_speed > 0.05:
        new_events = data.get("events", [])
        for e in new_events:
            record_event_to_history(
                e.get("type", "EVENT"),
                e.get("actor", "Unknown"),
                e.get("target", "None"),
                e.get("msg", ""),
                actor_faction=e.get("actor_faction", "None"),
                target_faction=e.get("target_faction", "None")
            )

    if not is_player_ctx:
        name = data.get("name")
        if name:
            get_persona_category(
                data.get("race"),
                data.get("faction") or data.get("factionID"),
                name=name,
                source="/context",
            )
            store_live_context(
                data,
                name=name,
                explicit_id=(
                    data.get("runtime_id")
                    or data.get("id")
                    or data.get("persistent_id")
                    or data.get("storage_id")
                ),
            )
            # Force update LAST_STATE_LOG for immediate debugger visibility
            with STATE_LOCK:
                LAST_STATE_LOG["npc"] = _parse_context_dict(data)
    return jsonify({"status": "ok"})


@app.route('/context', methods=['GET'])
def get_context():
    """Returns the most recent player and NPC context for the debugger/UI."""
    # Try to grab the last active NPC from live contexts
    last_npc = None
    if _istate.last_npc_key and _istate.last_npc_key in LIVE_CONTEXTS:
        last_npc = LIVE_CONTEXTS[_istate.last_npc_key]
    elif LIVE_CONTEXTS:
        last_npc_id = list(LIVE_CONTEXTS.keys())[-1]
        last_npc = LIVE_CONTEXTS[last_npc_id]
    
    # Use the global tracking for synthesis
    elapsed = SYNTHESIS_STATUS.get("elapsed", 0)
    interval = SYNTHESIS_STATUS.get("interval", 60)

    return jsonify({
        "status": "ok",
        "player": PLAYER_CONTEXT or LAST_STATE_LOG.get("player", {}),
        "selected": selected_speaker_status(),
        "effective_player": get_effective_player_context(),
        "npc": last_npc or LAST_STATE_LOG.get("npc", {}),
        "campaign": ACTIVE_CAMPAIGN,
        "synthesis": {
            "elapsed": elapsed,
            "interval": interval
        }
    })

@app.route('/rename_legacy', methods=['POST'])
def rename_endpoint():
    # Legacy alias path: delegate to the canonical /rename handler to avoid
    # divergent behavior and route-collision bugs.
    return rename_character()

@app.route('/settings', methods=['GET', 'POST'])
def settings_endpoint():
    global CURRENT_MODEL_KEY
    if request.method == 'POST' and request.content_length:
        logging.info(f"ROUTE: /settings [{request.method}]")
    else:
        logging.debug(f"ROUTE: /settings [{request.method}]")
    load_configs()

    # ---------- READ (GET or POST with no body) ----------
    data = None
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True)
        except:
            data = None

    if not data:
        # The C++ WelcomeWindow calls POST /settings with empty body to fetch config.
        # The visual_debugger calls GET /models. Both need the same response.
        settings = load_settings()
        r, t, y = get_config_radii()
        campaigns = [d for d in os.listdir(CAMPAIGNS_DIR) if os.path.isdir(os.path.join(CAMPAIGNS_DIR, d))] if os.path.exists(CAMPAIGNS_DIR) else []
        
        # Grouped map for dropdowns: Provider -> [Models]
        mbp = {}
        for k, v in MODELS_CONFIG.items():
            p = v.get("provider", "unknown")
            if p not in mbp: mbp[p] = []
            mbp[p].append(k)
        
        # Determine current provider
        curr_prov = MODELS_CONFIG.get(CURRENT_MODEL_KEY, {}).get("provider", "unknown")

        return jsonify({
            "status": "ok",
            "models": mbp,        # C++ dropdowns loop uses this
            "all_models": MODELS_CONFIG, # C++ initialization lookup
            "providers": list(PROVIDERS_CONFIG.keys()),
            "current": CURRENT_MODEL_KEY,
            "current_provider": curr_prov,
            "campaigns": campaigns,
            "current_campaign": ACTIVE_CAMPAIGN,
            "enable_ambient": settings.get("enable_ambient", True),
            "enable_renamer": settings.get("enable_renamer", True),
            "enable_animal_renamer": settings.get("enable_animal_renamer", True),
            "ambient_timer": settings.get("radiant_delay", 240),
            "synthesis_timer": settings.get("synthesis_interval_minutes", 15),
            "global_events_count": settings.get("global_events_count", 7),
            "dialogue_speed": settings.get("dialogue_speed_seconds", 5),
            "bubble_life": settings.get("bubble_life", 5),
            "chat_hotkey": settings.get("chat_hotkey", "\\"),
            "radii": {
                "radiant": settings.get("radiant_range", r),
                "talk": settings.get("talk_radius", t),
                "yell": settings.get("yell_radius", y)
            },
            "language": settings.get("language", "English"),
            "supported_languages": list(LOCALIZATION_CONFIG.keys()),
            "ui_translation": LOCALIZATION_CONFIG.get(settings.get("language", "English"), {})
        })

    # ---------- WRITE (POST with JSON body) ----------
    logging.info(f"Received settings update request: {json.dumps(data)}")
    changes = {}

    new_model = data.get("current_model")
    if new_model:
        if new_model in MODELS_CONFIG:
            CURRENT_MODEL_KEY = new_model
            set_current_model_key(CURRENT_MODEL_KEY)
            changes["current_model"] = CURRENT_MODEL_KEY
            logging.info(f"Model switched to: {CURRENT_MODEL_KEY}")
        else:
            msg = f"Invalid model '{new_model}'. Keeping current model '{CURRENT_MODEL_KEY}'."
            logging.warning(f"SETTINGS: {msg}")
            send_to_pipe(f"NOTIFY: SETTINGS ERROR - {msg}")
            return jsonify({
                "status": "error",
                "message": msg,
                "current_model": CURRENT_MODEL_KEY
            }), 400

    enable_ambient = data.get("enable_ambient")
    if enable_ambient is not None:
        changes["enable_ambient"] = enable_ambient
        send_to_pipe(f"SET_CONFIG: g_enableAmbient: {'1' if enable_ambient else '0'}")
        logging.info(f"Ambient enabled set to: {enable_ambient}")

    enable_renamer = data.get("enable_renamer")
    if enable_renamer is not None:
        changes["enable_renamer"] = enable_renamer
        send_to_pipe(f"SET_CONFIG: g_enableRenamer: {'1' if enable_renamer else '0'}")
        logging.info(f"Renamer enabled set to: {enable_renamer}")

    enable_animal_renamer = data.get("enable_animal_renamer")
    if enable_animal_renamer is not None:
        changes["enable_animal_renamer"] = enable_animal_renamer
        send_to_pipe(f"SET_CONFIG: g_enableAnimalRenamer: {'1' if enable_animal_renamer else '0'}")
        logging.info(f"Animal Renamer enabled set to: {enable_animal_renamer}")

    ambient_timer = data.get("ambient_timer")
    if ambient_timer is not None:
        try:
            val = int(ambient_timer)
            changes["radiant_delay"] = val
            send_to_pipe(f"SET_CONFIG: g_ambientIntervalSeconds: {val}")
            logging.info(f"Radiant delay set to: {val}")
        except (ValueError, TypeError): pass

    radii = data.get("radii")
    if radii:
        r = radii.get("radiant")
        t = radii.get("talk")
        y = radii.get("yell")
        if r is not None:
            send_to_pipe(f"SET_CONFIG: g_radiantRange: {r}")
        if t is not None:
            send_to_pipe(f"SET_CONFIG: g_talkRadius: {t}")
        if y is not None:
            send_to_pipe(f"SET_CONFIG: g_yellRadius: {y}")
        changes["radii"] = radii

    min_rel = data.get("min_faction_relation")
    if min_rel is not None:
        send_to_pipe(f"SET_CONFIG: g_minFactionRelation: {min_rel}")
        changes["min_faction_relation"] = min_rel

    lang = data.get("language")
    if lang is not None:
        changes["language"] = lang
        logging.info(f"Language set to: {lang}")

    max_rel = data.get("max_faction_relation")
    if max_rel is not None:
        send_to_pipe(f"SET_CONFIG: g_maxFactionRelation: {max_rel}")
        changes["max_faction_relation"] = max_rel

    ge_count = data.get("global_events_count")
    if ge_count is not None:
        try:
            val = int(ge_count)
            changes["global_events_count"] = val
            logging.info(f"Global events count set to: {val}")
        except (ValueError, TypeError):
            logging.warning(f"SETTINGS: invalid value for global_events_count: {ge_count!r}, ignoring")

    syn_timer = data.get("synthesis_timer")
    if syn_timer is not None:
        try:
            val = int(syn_timer)
            changes["synthesis_interval_minutes"] = val
            logging.info(f"Synthesis timer set to: {val} minutes")
        except (ValueError, TypeError):
            logging.warning(f"SETTINGS: invalid value for synthesis_timer: {syn_timer!r}, ignoring")

    diag_speed = data.get("dialogue_speed")
    if diag_speed is not None:
        try:
            val = int(diag_speed)
            changes["dialogue_speed_seconds"] = val
            send_to_pipe(f"SET_CONFIG: g_dialogueSpeedSeconds: {val}")
            logging.info(f"Dialogue speed set to: {val} seconds")
        except (ValueError, TypeError):
            logging.warning(f"SETTINGS: invalid value for dialogue_speed: {diag_speed!r}, ignoring")

    bubble_life = data.get("bubble_life")
    if bubble_life is not None:
        try:
            val = float(bubble_life)
            changes["bubble_life"] = val
            send_to_pipe(f"SET_CONFIG: g_speechBubbleLife: {val}")
            logging.info(f"Bubble life set to: {val} seconds")
        except (ValueError, TypeError):
            logging.warning(f"SETTINGS: invalid value for bubble_life: {bubble_life!r}, ignoring")

    chat_hotkey = data.get("chat_hotkey")
    if chat_hotkey is not None:
        changes["chat_hotkey"] = str(chat_hotkey)
        logging.info(f"Chat hotkey set to: {chat_hotkey}")

    campaign = data.get("current_campaign")
    if campaign:
        if switch_campaign(campaign):
            changes["current_campaign"] = ACTIVE_CAMPAIGN
            logging.info(f"Campaign switched to: {ACTIVE_CAMPAIGN}")

    if changes:
        save_settings(changes)
        logging.info(f"Successfully saved {len(changes)} setting changes.")
        return jsonify({"status": "ok", **changes})

    return jsonify({"status": "error", "message": "No valid settings provided"}), 400

@app.route('/campaigns/list', methods=['GET'])
def list_campaigns_route():
    logging.info("ROUTE: /campaigns/list [GET]")
    # Opportunistic Kayak reconnect at natural UI checkpoint
    if not KAYAK_ENABLED and (time.monotonic() - _KAYAK_LAST_RETRY > 60.0):
        logging.info("KAYAK: Opportunistic reconnect from /campaigns/list...")
        _kayak_try_connect()
    if not os.path.exists(CAMPAIGNS_DIR):
        os.makedirs(CAMPAIGNS_DIR)
    
    # Ensure Default exists
    d_dir = os.path.join(CAMPAIGNS_DIR, "Default")
    if not os.path.exists(d_dir): os.makedirs(d_dir)
        
    camps = [d for d in os.listdir(CAMPAIGNS_DIR) if os.path.isdir(os.path.join(CAMPAIGNS_DIR, d))]
    return jsonify({"status": "ok", "campaigns": camps, "current": ACTIVE_CAMPAIGN})

@app.route('/campaigns/create', methods=['POST'])
def create_campaign_route():
    logging.info("ROUTE: /campaigns/create [POST]")
    data = request.json
    name = data.get("name")
    if not name: return jsonify({"status": "error", "message": "Missing name"}), 400
    
    # Sanitize
    safe_name = "".join([c for c in name if c.isalnum() or c in (' ', '_', '-')]).strip()
    if not safe_name: return jsonify({"status": "error", "message": "Invalid name"}), 400
    
    cdir = os.path.join(CAMPAIGNS_DIR, safe_name)
    if os.path.exists(cdir):
        return jsonify({"status": "error", "message": "Campaign already exists"}), 400
        
    os.makedirs(cdir)
    ensure_campaign_seeded(cdir)
    
    # Automatically switch to the new campaign
    switch_campaign(safe_name)
            
    logging.info(f"CAMPAIGN: Created and switched to new campaign '{safe_name}'")
    return jsonify({"status": "ok", "name": safe_name, "current": ACTIVE_CAMPAIGN})

@app.route('/campaigns/switch', methods=['POST'])
def switch_campaign_route():
    logging.info("ROUTE: /campaigns/switch [POST]")
    data = request.json
    name = data.get("name")
    if not name: return jsonify({"status": "error", "message": "Missing name"}), 400
    if switch_campaign(name):
        return jsonify({"status": "ok", "current": ACTIVE_CAMPAIGN})
    return jsonify({"status": "error", "message": "Campaign not found"}), 404

@app.route('/campaigns/cull', methods=['POST'])
def cull_campaign_route():
    """
    Cull future NPC dialogue lines after the current/explicit game timestamp.

    Contract:
    - Kayak performs the filesystem dialogue trim.
    - Kayak reloads/reindexes the active campaign before success.
    - No legacy fallback is used, because fallback hides broken culls.
    """
    logging.info("ROUTE: /campaigns/cull [POST]")
    data = request.get_json(silent=True) or {}

    def _read_int(name, fallback=None):
        value = data.get(name, fallback)
        if value in (None, ""):
            raise ValueError(name)
        return int(value)

    try:
        current_day = _read_int("day", PLAYER_CONTEXT.get("day"))
        current_hour = _read_int("hour", PLAYER_CONTEXT.get("hour"))
        current_min = _read_int("minute", PLAYER_CONTEXT.get("minute"))
    except (TypeError, ValueError):
        msg = "CULL: Missing or invalid day/hour/minute. Wait for player context update, or send explicit cutoff."
        logging.error(msg)
        return jsonify({"status": "error", "error": msg}), 400

    if current_day < 0 or not (0 <= current_hour <= 23) or not (0 <= current_min <= 59):
        msg = f"CULL: Invalid cutoff Day {current_day}, {current_hour:02d}:{current_min:02d}"
        logging.error(msg)
        return jsonify({"status": "error", "error": msg}), 400

    dry_run = bool(data.get("dry_run", False))
    reload_after = bool(data.get("reload_after", True))

    logging.info(
        "CULL: Request cutoff=[Day %s, %02d:%02d] campaign=%s dry_run=%s reload_after=%s source=%s",
        current_day,
        current_hour,
        current_min,
        ACTIVE_CAMPAIGN,
        dry_run,
        reload_after,
        "request" if any(k in data for k in ("day", "hour", "minute")) else "PLAYER_CONTEXT",
    )

    if not (KAYAK_ENABLED and kayak and hasattr(kayak, "cull_future_dialogue")):
        msg = "CULL: Kayak is not available; refusing to run legacy fallback."
        logging.error(msg)
        return jsonify({"status": "error", "error": msg}), 503

    try:
        kayak_result = kayak.cull_future_dialogue(
            day=current_day,
            hour=current_hour,
            minute=current_min,
            campaign=ACTIVE_CAMPAIGN,
            dry_run=dry_run,
            reload_after=reload_after,
        ) or {}
    except Exception as e:
        msg = f"CULL: Kayak dialogue cull exception: {e}"
        logging.exception(msg)
        return jsonify({"status": "error", "error": msg}), 500

    if kayak_result.get("status") != "ok":
        logging.error(f"CULL: Kayak dialogue cull failed: {kayak_result}")
        return jsonify(kayak_result), 500

    # Kayak has reloaded its own index before returning success. Also clear the
    # SentientSands character cache so the next prompt reloads fresh dialogue.
    try:
        character_gateway.invalidate()
    except Exception as e:
        logging.warning(f"CULL: character_gateway.invalidate failed: {e}")

    logging.info(
        "CULL: Complete cutoff=%s files_changed=%s lines_removed=%s kayak_reloaded=%s dry_run=%s",
        kayak_result.get("cutoff", {}).get("label"),
        kayak_result.get("files_changed", 0),
        kayak_result.get("lines_removed", 0),
        kayak_result.get("kayak_reloaded", False),
        kayak_result.get("dry_run", False),
    )
    return jsonify(kayak_result)

def switch_campaign(name):
    global ACTIVE_CAMPAIGN
    # Opportunistic Kayak reconnect at campaign switch
    if not KAYAK_ENABLED and (time.monotonic() - _KAYAK_LAST_RETRY > 60.0):
        logging.info("KAYAK: Opportunistic reconnect from switch_campaign...")
        _kayak_try_connect()
    cdir = os.path.join(CAMPAIGNS_DIR, name)
    if os.path.exists(cdir):
        ACTIVE_CAMPAIGN = name
        save_settings({"current_campaign": name})  # Persist across restarts
        _clear_campaign_runtime_state()
        _load_active_campaign_runtime_state()
        populate_initial_registry()
        # added by Pineaxe v04 - tell Kayak to switch campaign context
        if KAYAK_ENABLED:
            _ss_campaign_dir = os.path.join(CAMPAIGNS_DIR, name)
            kayak.on_save_loaded(name, ss_campaign_dir=_ss_campaign_dir)
            # Sync character registry from Kayak (fast one-time operation)
            if _HAVE_CHARACTER_HANDLER:
                try:
                    sync_registry_from_kayak(campaign=name, kayak_bridge=kayak)
                except Exception as _sync_e:
                    logging.warning(f"Character registry sync failed: {_sync_e}")
        return True
    return False

@app.route('/history', methods=['POST'])
def get_history():
    logging.info("ROUTE: /history [POST]")
    data = request.json or {}
    
    # Accept both 'npc' (from Library) and 'name' (from older calls)
    npc_name = data.get('npc', data.get('name', 'Someone'))
    sid_hint = str(data.get('sid') or data.get('storage_id') or '').strip() or None
    
    logging.info(f"HISTORY: Request for {npc_name}")
    
    # CRITICAL: Clean the name from pipes (serial IDs) before any lookup.
    clean_npc_name = npc_name.split('|')[0] if '|' in npc_name else npc_name
    clean_npc_name = str(clean_npc_name).strip()
    context = data.get('context', '')
    
    # Resolve identity from context
    char_data = None
    ident = _context_identity_summary(context, fallback_name=clean_npc_name)
    strength = ident.get("strength", 0)

    # /history is an inspect/debug style endpoint, so prefer a fresh reload from
    # Kayak instead of a potentially stale name-only cache entry.
    resolved_sid = sid_hint or ident.get("storage_id")
    if resolved_sid:
        char_data = character_gateway.reload(clean_npc_name, resolved_sid, ACTIVE_CAMPAIGN)
        if char_data:
            logging.info(f"HISTORY: Resolved {clean_npc_name} via fresh storage_id reload {resolved_sid}")

    # Legacy DLL compatibility: some builds send {"npc":"<storage_id>"} instead
    # of a real name plus sid. If the first pass had no explicit sid, retry by
    # treating the npc field itself as the persistent/storage id.
    if not char_data and not resolved_sid and clean_npc_name:
        char_data = character_gateway.reload(clean_npc_name, clean_npc_name, ACTIVE_CAMPAIGN)
        if char_data:
            logging.info(
                f"HISTORY: Resolved legacy id-only request via fresh storage_id reload {clean_npc_name}"
            )

    if not char_data and strength == 1:
        char_data = character_gateway.reload(clean_npc_name, None, ACTIVE_CAMPAIGN)
        if char_data:
            logging.info(f"HISTORY: Resolved {clean_npc_name} via fresh gateway name reload")

    # Fallback to standard resolution (which also uses gateway internally).
    # skip_generate=True: /history must never create a new profile.
    if not char_data:
        logging.debug(f"HISTORY: Falling back to get_character_data for {clean_npc_name}")
        char_data = get_character_data(clean_npc_name, context, skip_generate=True)
    
    # Schema safety net
    if "ConversationHistory" not in char_data: char_data["ConversationHistory"] = []
    if "Race" not in char_data: char_data["Race"] = "Unknown"
    if "Faction" not in char_data: char_data["Faction"] = "Unknown"
    
    # Return full history as requested
    history = char_data.get('ConversationHistory', [])

    def _wrap(text):
        if not text: return ""
        paragraphs = text.split('\n')
        wrapped = []
        for p in paragraphs:
            if not p.strip():
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(p, width=110))
        return "\n".join(wrapped)
        
    lines = []
    lines.append(f"--- PROFILE: {char_data.get('Name', clean_npc_name)} ---")
    lines.append(f"Faction: {char_data.get('Faction', 'Unknown')} | Race: {char_data.get('Race', 'Unknown')}")
    lines.append(generate_relation_bar(char_data.get('Relation', 0)))
    lines.append("-" * 30)
    if profile_needs_upgrade(char_data):
        lines.append("STATUS:")
        lines.append("You don't know this person yet.")
        observed = _pending_observation_lines(char_data, context)
        if observed:
            lines.append("")
            lines.append("OBSERVED DETAILS:")
            for observed_line in observed:
                lines.append(_wrap(observed_line))
    else:
        lines.append("PERSONALITY:")
        lines.append(_wrap(char_data.get('Personality', 'Unknown')))
        lines.append("")
        lines.append("BACKSTORY:")
        lines.append(_wrap(char_data.get('Backstory', 'Unknown')))
    lines.append("-" * 30)
    lines.append(f"CONVERSATION LOG (Showing last 250 of {len(history)} lines):")
    if history:
        # Limit display to 250 lines to prevent UI freeze
        trimmed_history = history[-250:]
        for log_line in trimmed_history:
            lines.append(_wrap(log_line))
    else:
        lines.append("(No history recorded)")
        
    formatted_output = "\n".join(lines)
    
    logging.info(f"HISTORY: Returning formatted report for {clean_npc_name} ({len(history)} lines)")
    return jsonify({
        "status": "ok",
        "text": formatted_output
    })

@app.route('/characters', methods=['GET', 'POST'])
def list_characters():
    data = request.json or {}
    sort_mode = data.get("sort", "alphabetical") # alphabetical or latest
    
    settings = load_settings()
    favorites = settings.get("favorites", [])

    npc_list = []

    # Gateway / Kayak list path (source of truth)
    if KAYAK_ENABLED and kayak:
        try:
            all_fields = kayak.get_all_npc_fields(ACTIVE_CAMPAIGN)
            for display_name, field_dict in (all_fields or {}).items():
                safe_fields = field_dict or {}
                resolved_display = str(
                    safe_fields.get("display_name")
                    or display_name
                    or ""
                ).strip()
                sid = str(
                    safe_fields.get("persistent_id")
                    or safe_fields.get("id")
                    or safe_fields.get("runtime_id")
                    or resolved_display
                ).strip()
                if not sid or not resolved_display:
                    continue

                is_fav = sid in favorites
                profile_state = str(safe_fields.get("profile_state") or "").strip().lower()
                has_dialogue = _string_truthy(safe_fields.get("has_dialogue"))
                dialogue_path = _npc_dialogue_path(ACTIVE_CAMPAIGN, safe_fields, resolved_display)
                if not has_dialogue and dialogue_path:
                    if os.path.isfile(dialogue_path):
                        try:
                            has_dialogue = os.path.getsize(dialogue_path) > 0
                        except OSError:
                            has_dialogue = False

                pending_stub = is_placeholder_profile({
                    "Personality": safe_fields.get("personality", ""),
                    "Backstory": safe_fields.get("backstory", ""),
                    "_profile_state": profile_state,
                })
                if pending_stub and not has_dialogue and not is_fav:
                    continue

                npc_list.append({
                    "display": resolved_display,
                    "sid": sid,
                    "mtime": float(safe_fields.get("mtime", 0)),
                    "is_fav": is_fav,
                })
        except Exception as e:
            logging.warning(f"CHARACTERS: Kayak list failed: {e}")

    # Deduplicate by storage ID — same NPC cannot appear twice, but same-name NPCs
    # remain distinct entries.
    unique_npcs = {}
    for n in npc_list:
        sid = n["sid"]
        if sid not in unique_npcs or n["mtime"] > unique_npcs[sid]["mtime"]:
            unique_npcs[sid] = n

    final_list = list(unique_npcs.values())

    # Sorting logic
    if sort_mode == "latest":
        final_list.sort(key=lambda x: x["mtime"], reverse=True)
    else:
        final_list.sort(key=lambda x: x["display"].lower())

    # Favorites always on top
    favs = [n for n in final_list if n["is_fav"]]
    others = [n for n in final_list if not n["is_fav"]]
    
    sorted_npcs = favs + others
    
    names = [f"{n['display']}|{n['sid']}" for n in sorted_npcs]
    
    return jsonify({
        "status": "ok",
        "characters": ",".join(names),
        "names": ",".join(names),
        "favorites": favorites
    })

@app.route('/favorite', methods=['POST'])
def toggle_favorite():
    data = request.json or {}
    sid = data.get("sid")
    if not sid:
        return jsonify({"status": "error"}), 400
    
    settings = load_settings()
    favorites = settings.get("favorites", [])
    
    if sid in favorites:
        favorites.remove(sid)
        status = "removed"
    else:
        favorites.append(sid)
        status = "added"
    
    settings["favorites"] = favorites
    save_settings(settings)
    
    return jsonify({"status": "ok", "state": status})
@app.route('/player_profile', methods=['GET', 'POST'])
def player_profile_route():
    # Robust handling for C++ client sending empty JSON body
    data = None
    if request.is_json:
        try:
            data = request.get_json(silent=True)
        except:
            pass
    
    # If GET, or POST with no usable JSON (loading call)
    if request.method == 'GET' or not data:
        logging.info("PROMPT: Loading player profile (GUI request).")
        bio = load_prompt_component("character_bio.txt", "A mysterious drifter.")
        faction = load_prompt_component("player_faction_description.txt", "")
        return jsonify({
            "status": "ok",
            "character_bio": bio,
            "player_faction": faction
        })
    else:
        # Save
        bio = data.get("character_bio")
        faction = data.get("player_faction")
        
        cdir = get_campaign_dir()
        if bio is not None:
            with open(os.path.join(cdir, "character_bio.txt"), "w", encoding="utf-8") as f:
                f.write(bio)
        if faction is not None:
            with open(os.path.join(cdir, "player_faction_description.txt"), "w", encoding="utf-8") as f:
                f.write(faction)
        
        logging.info("PROMPT: Player profile updated via UI.")
        return jsonify({"status": "ok"})

@app.route('/test_connection', methods=['POST'])
def test_connection():
    logging.info("Testing LLM connection...")
    test_prompt = [{"role": "user", "content": "You are a Kenshi NPC. Say 'Connection Successful!' in a very short way."}]
    try:
        response = call_llm(test_prompt, max_tokens=20)
        if response:
            logging.info(f"Test Successful: {response}")
            return f"NOTIFY: Connection Successful! AI says: {response}", 200
        else:
            return "NOTIFY: ERROR: No response from AI. Check your API key and Provider settings.", 200
    except Exception as e:
        logging.error(f"Test Failed: {e}")
        return f"NOTIFY: ERROR: {str(e)}", 200

@app.route('/reset', methods=['POST'])
def reset_server():
    logging.info("Resetting server state...")
    try:
        clear_live_context_cache()
        _clear_pending_profiles()
        load_configs()
        build_world_index() 
        logging.info("Server reset complete (Cache cleared, configs reloaded).")
        return "NOTIFY: Server Reset Complete (Identity cache cleared and configs reloaded).", 200
    except Exception as e:
        return f"NOTIFY: Reset failed: {str(e)}", 200

def synthesis_loop():
    """Background loop to periodically synthesize world rumors."""
    logging.info("NARRATIVE: Synthesis background loop started.")
    elapsed_minutes = 0
    while True:
        try:
            settings = load_settings()
            interval = settings.get("synthesis_interval_minutes", 15)
            if interval < 1: interval = 1 # Safety
            
            SYNTHESIS_STATUS["interval"] = interval
            
            # If interval was shortened below current elapsed, trigger now
            if elapsed_minutes >= interval:
                logging.info(f"NARRATIVE: Interval shortened ({interval}m). Triggering synthesis.")
                _queue_auto_synthesis()
                elapsed_minutes = 0
                SYNTHESIS_STATUS["elapsed"] = 0
                continue

            # Sleep in smaller chunks to be responsive to game state changes
            for _ in range(6): # 6 × 10s = ~60s per loop iteration
                time.sleep(10)

            # After ~60s of real time, check if game was running
            speed = PLAYER_CONTEXT.get("gamespeed", 1.0)
            
            if speed > 0.1:
                elapsed_minutes += 1
                SYNTHESIS_STATUS["elapsed"] = elapsed_minutes
                if elapsed_minutes % 10 == 0:
                    logging.info(f"NARRATIVE: Timer progress: {elapsed_minutes}/{interval} minutes.")
            
            if elapsed_minutes >= interval:
                logging.info(f"NARRATIVE: Timer reached ({interval}m). Triggering periodic synthesis.")
                _queue_auto_synthesis()
                elapsed_minutes = 0
                SYNTHESIS_STATUS["elapsed"] = 0
                    
        except Exception as e:
            logging.error(f"Error in synthesis loop: {e}")
            time.sleep(60)

# Start synthesis thread
threading.Thread(target=synthesis_loop, daemon=True).start()

def deferred_profile_flush_loop():
    """Periodically flush deferred profile batches when foreground chat is idle."""
    while True:
        try:
            flushed = flush_deferred_profile_batches()
            if flushed:
                logging.info(f"BATCH: Flushed {flushed} deferred profiles after direct chat.")
        except Exception as e:
            logging.error(f"Error in deferred profile flush loop: {e}")
        time.sleep(2)

# Start deferred profile flush thread
threading.Thread(target=deferred_profile_flush_loop, daemon=True).start()

def player2_ping_loop():
    """Periodically pings player2 server and refreshes p2Key if it is the active provider."""
    global PLAYER2_SESSION_KEY
    # Use debug for the thread start to stay out of the way for non-p2 users
    logging.debug("HEALTH: Player2 background thread initialized.")
    game_id = "019c93fc-7a93-7ac4-8c6e-df0fd09bec01"
    
    while True:
        try:
            model_entry = MODELS_CONFIG.get(CURRENT_MODEL_KEY)
            if model_entry and model_entry.get("provider") == "player2":
                # 1. Quick Start: Attempt to fetch fresh p2Key from local Player2 App
                # ONLY if we don't already have one (Beginning of session/usage)
                if not PLAYER2_SESSION_KEY:
                    try:
                        auth_url = f"http://localhost:4315/v1/login/web/{game_id}"
                        auth_resp = requests.post(auth_url, timeout=5)
                        if auth_resp.status_code == 200:
                            new_key = auth_resp.json().get("p2Key")
                            if new_key:
                                PLAYER2_SESSION_KEY = new_key
                                set_player2_session_key(PLAYER2_SESSION_KEY)

                                logging.info("HEALTH: Player2 session authorized at startup.")
                    except Exception as e:
                        # App might not be running or not logged in; silently fall back
                        pass


                # 2. Ping /health as a health check
                provider_config = PROVIDERS_CONFIG.get("player2")
                if provider_config:
                    base_url = (provider_config.get("base_url") or "").rstrip("/")
                    try:
                        # Use player2-game-key header and Authorization for health check
                        h = {
                            "player2-game-key": game_id,
                            "Authorization": f"Bearer {PLAYER2_SESSION_KEY}" if PLAYER2_SESSION_KEY else ""
                        }
                        resp = requests.get(f"{base_url}/health", headers=h, timeout=5)
                        if resp.status_code == 200:
                            logging.debug("HEALTH: Player2 server is UP")
                        else:
                            logging.warning(f"HEALTH: Player2 server returned status {resp.status_code}")
                    except Exception as e:
                        logging.error(f"HEALTH: Player2 server is DOWN or unreachable: {e}")
            
        except Exception as e:
            logging.error(f"Error in player2 background thread: {e}")
        
        time.sleep(60)

# Start player2 ping thread
threading.Thread(target=player2_ping_loop, daemon=True).start()

def monitor_kenshi_process():
    """Background thread that monitors the parent process (Kenshi) and exits if it's gone."""
    try:
        ppid = _get_monitored_parent_pid()
        if ppid <= 1:
            logging.info("SYSTEM: Parent PID is 0 or 1, skipping auto-shutdown monitor.")
            return
            
        logging.info(f"SYSTEM: Monitoring parent process (PID {ppid}) for auto-shutdown.")
        
        # Windows constants
        PROCESS_QUERY_INFORMATION = 0x0400
        STILL_ACTIVE = 259
        
        # Use ctypes for more reliable process checking on Windows
        kernel32 = ctypes.windll.kernel32
        
        while True:
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, ppid)
            if not handle:
                # If we can't open it, the process is likely gone
                _shutdown_server(f"SYSTEM: Parent Kenshi process (PID {ppid}) no longer found. Shutting down server.")
                 
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != STILL_ACTIVE:
                    kernel32.CloseHandle(handle)
                    _shutdown_server(f"SYSTEM: Parent Kenshi process (PID {ppid}) has exited. Shutting down server.")
            else:
                # GetExitCodeProcess failed, might be gone
                kernel32.CloseHandle(handle)
                _shutdown_server("SYSTEM: Failed to query parent process state. Assuming it closed. Shutting down server.")
            
            kernel32.CloseHandle(handle)
            time.sleep(5) 
            
    except Exception as e:
        logging.error(f"SYSTEM: Error in kenshi process monitor: {e}")

# Start Kenshi monitor thread
threading.Thread(target=monitor_kenshi_process, daemon=True).start()


# --- WEB DEBUGGER ROUTES (v2.0) ---
from flask import render_template, send_from_directory

@app.route('/debugger')
def serve_debugger():
    """Serve the modern web-based visual debugger."""
    return render_template('debugger.html')

@app.route('/status')
def status():
    return jsonify({
        "status": "online",
        "active_campaign": ACTIVE_CAMPAIGN,
        "active_model": CURRENT_MODEL_KEY
    })

@app.route('/kayak_status')
def kayak_status():
    is_avail = character_gateway.available
    return jsonify({
        "status": "online" if is_avail else "offline",
        "api_healthy": is_avail,
        "circuit_broken": getattr(kayak_hub, "_circuit_broken", False) if kayak_hub else False
    })

@app.route('/models', methods=['GET'])
def get_models_alias():
    """Alias for settings endpoint to satisfy web debugger."""
    return settings_endpoint()

@app.route('/api/command', methods=['POST'])
def web_command():
    """Relay commands from the web UI to the Kenshi pipe."""
    data = request.json or {}
    cmd = data.get('command')
    if not cmd:
        return jsonify({"status": "error", "message": "Missing command"}), 400
    
    # Handle specialized web commands
    if cmd == "MANUAL_SYNTHESIZE":
        generate_global_narrative_thread()
        return jsonify({"status": "ok", "message": "Synthesis triggered"})
    elif cmd == "RESCAN_SAVES":
        update_world_index()
        return jsonify({"status": "ok", "message": "Save index updated"})
    elif cmd == "SOFT_RESET_SERVER":
        clear_live_context_cache()
        _clear_pending_profiles()
        LAST_STATE_LOG.clear()
        EVENT_THROTTLE.clear()
        return jsonify({"status": "ok", "message": "Server state reset"})
    elif cmd in ("RESET_SERVER", "RESTART_SERVER", "RESTART"):
        logging.info(f"SYSTEM: Restart requested via command '{cmd}'.")
        _restart_self_async(f"SYSTEM: Restart requested via command '{cmd}'. Shutting down old process.")
        return jsonify({"status": "ok", "message": "Server restarting"})
    
    # Standard pipe relay
    send_to_pipe(cmd)
    logging.info(f"WEB_CMD: Relayed command: {cmd}")
    return jsonify({"status": "ok"})

@app.route('/restart', methods=['POST'])
def restart_route():
    """HTTP restart hook for in-game/tools buttons."""
    logging.info("SYSTEM: Restart requested via /restart endpoint.")
    _restart_self_async("SYSTEM: Restart requested via /restart. Shutting down old process.")
    return jsonify({"status": "ok", "message": "Server restarting"})

@app.route('/api/test_trade', methods=['POST'])
def test_trade_alias():
    """Endpoint for debugger to test item normalization"""
    data = request.json or {}
    item = data.get('item', '')
    if not item:
        return jsonify({"status": "error", "message": "Missing item name"}), 400
    normalized = normalize_trade_item_name(item)
    return jsonify({"status": "ok", "original": item, "normalized": normalized})

@app.route('/api/logs/<path:log_name>')
def stream_logs(log_name):
    """Serve log files for the real-time event feed with correct MIME type."""
    if ".." in log_name:
        return "Access Denied", 403

    # Map global_events.log (used by debugger) to world_events.txt
    actual_log_name = log_name
    if log_name == "global_events.log":
        actual_log_name = "world_events.txt"
        
    # Priority 1: server.log or llm_debug.log (the main tool/app logs)
    if actual_log_name in ["server.log", "llm_debug.log"]:
        log_path = os.path.join(KENSHI_SERVER_DIR, "logs", actual_log_name)
        if os.path.exists(log_path):
            resp = send_from_directory(os.path.dirname(log_path), os.path.basename(log_path))
            resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp

    # Priority 2: campaign-specific logs/events
    camp_dir = get_campaign_dir()
    # Check campaign/logs/
    log_dir = os.path.join(camp_dir, "logs")
    if os.path.exists(os.path.join(log_dir, actual_log_name)):
        resp = send_from_directory(log_dir, actual_log_name)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return resp
    # Check campaign root (for world_events.txt)
    elif os.path.exists(os.path.join(camp_dir, actual_log_name)):
        resp = send_from_directory(camp_dir, actual_log_name)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return resp
        
    # Priority 3: general logs fallback
    fallback_dir = os.path.join(KENSHI_SERVER_DIR, "logs")
    if os.path.exists(os.path.join(fallback_dir, actual_log_name)):
        resp = send_from_directory(fallback_dir, actual_log_name)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return resp

    return "File Not Found", 404

if __name__ == '__main__':
    logging.info("Kenshi LLM Server Starting on port 5000...")
    # Enable threaded=True to handle multiple simultaneous requests (polling + settings)
    app.run(host='127.0.0.1', port=5000, threaded=True)
