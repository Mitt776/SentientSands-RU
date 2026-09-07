# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak background server.

Start:  python kayak_server.py
Default: http://127.0.0.1:5001

Endpoints
─────────
GET  /status
GET  /campaign/list
POST /campaign/create        {name}
POST /campaign/load          {name}
POST /campaign/switch        {name}           ← create-or-load in one call
POST /campaign/reload_index
POST /config/reload

POST /prompt/chat            build chat prompt
POST /prompt/loremaster      build loremaster prompt
POST /prompt/biography       build biography prompt
POST /prompt/radiant         build radiant group prompt

POST /write/npc              write entity.txt for an NPC (+ incremental reindex)
POST /rename/npc             rename an NPC entity folder + sync name fields
POST /write/stats            write stats.txt  (no reindex — prompt-time read)
POST /write/world_event      append a world event entity
POST /write/lore_chunk       write a single lore entity

POST /dialogue/save          persist a completed dialogue exchange
POST /dialogue/cull_future   remove dialogue lines after a supplied game timestamp
POST /events/parse           clean + split raw event log
"""

import os
import re
import sys
import hashlib
import logging
import threading
import pathlib

KAYAK_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KAYAK_ROOT)

from flask import Flask, request, jsonify

from scripts.campaign_manager import CampaignManager
from scripts.retriever        import Retriever, extract_keywords
from scripts.prompt_builder   import (
    PromptPolicy, POLICY_CHAT, POLICY_CHAT_WHISPER,
    POLICY_CHAT_YELL, POLICY_CHAT_ANIMAL, POLICY_CHAT_MACHINE, POLICY_CHAT_FERAL, POLICY_CHAT_SAPIENT,
    POLICY_LOREMASTER, POLICY_BIOGRAPHY, POLICY_BIOGRAPHY_ANIMAL,
    POLICY_BIOGRAPHY_MACHINE, POLICY_BIOGRAPHY_FERAL, POLICY_BIOGRAPHY_SAPIENT, POLICY_CHAT_RADIANT, POLICY_SPEAK,
)
from scripts.token_resolver   import TokenResolver, TokenResolverContext
from scripts.event_parser     import parse_event_log
from scripts.error_reporter   import setup_error_report_logger, report_error, report_warning

logging.basicConfig(
    level  = logging.INFO,
    format = "[Kayak] %(levelname)s  %(message)s",
)
log = logging.getLogger("kayak")
error_report_log = setup_error_report_logger(KAYAK_ROOT)
log.info(f"Kayak error report log: {error_report_log}")

app   = Flask("Kayak")
cm    = CampaignManager(KAYAK_ROOT)
_lock = threading.RLock()

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _get_retriever() -> Retriever:
    return Retriever(cm.indexer, cm.config)

def _need_campaign():
    if cm.active_name is None:
        return jsonify({"error": "No active campaign. POST /campaign/load first."}), 503
    return None

def _maybe_switch(data: dict):
    name = (data.get("campaign") or "").strip()
    if name and name != cm.active_name:
        with _lock:
            if not cm.campaign_exists(name):
                cm.create_campaign(name)
            cm.load_campaign(name)

def _require_active_campaign(data: dict):
    try:
        _maybe_switch(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _need_campaign()

def _safe_segment(value: str, field: str) -> str:
    segment = str(value or "").strip()
    if not segment:
        raise ValueError(f"'{field}' is required")
    if segment in (".", ".."):
        raise ValueError(f"Invalid {field}")
    if "/" in segment or "\\" in segment or "\x00" in segment:
        raise ValueError(f"Invalid {field}: path separators are not allowed")
    return segment

def _safe_field_key(value: str) -> str:
    field = str(value or "").strip()
    if not field:
        raise ValueError("'field' is required")
    if any(ch in field for ch in ("\r", "\n", "=", "\x00")):
        raise ValueError("Invalid field name")
    return field

def _safe_keep_lines(raw_value, default_value: int) -> int:
    try:
        keep = int(raw_value) if raw_value not in (None, "") else int(default_value)
    except (TypeError, ValueError):
        keep = int(default_value)
    if keep < 0:
        keep = int(default_value)
    return keep

_DIALOGUE_TIMESTAMP_RE = re.compile(r"\[Day (\d+)(?:, (\d+):(\d+))?\]", re.IGNORECASE)
_UNIQUE_NPC_MERGE_FIELDS = (
    "$personality",
    "$backstory",
    "$speech_quirks",
    "$knows_about",
)

def _dialogue_line_is_future(line: str, cutoff_day: int, cutoff_hour: int, cutoff_minute: int) -> bool:
    match = _DIALOGUE_TIMESTAMP_RE.search(str(line or ""))
    if not match:
        return False
    day = int(match.group(1))
    hour = int(match.group(2)) if match.group(2) is not None else 0
    minute = int(match.group(3)) if match.group(3) is not None else 0
    if day != cutoff_day:
        return day > cutoff_day
    if hour != cutoff_hour:
        return hour > cutoff_hour
    return minute > cutoff_minute

def _assert_path_within(root: pathlib.Path, target: pathlib.Path, field: str):
    root_abs = root.resolve(strict=False)
    target_abs = target.resolve(strict=False)
    try:
        common = os.path.commonpath([str(root_abs), str(target_abs)])
    except ValueError:
        raise ValueError(f"Invalid {field}: path escapes base directory")
    if common != str(root_abs):
        raise ValueError(f"Invalid {field}: path escapes base directory")

def _resolve_target(data: dict):
    """
    Resolve target NPC from request data.
    Prefers persistent_id > runtime_id > name.
    Returns (entity, uid) or (None, None).
    """
    idx = cm.indexer
    if not idx:
        return None, None

    # Try IDs first (most reliable)
    requested = []
    for id_key in ("target_npc_id", "persistent_id", "runtime_id"):
        val = (data.get(id_key) or "").strip()
        if val:
            requested.append(val)
            uid = idx.find_by_id(val)
            if uid:
                return idx.get(uid), uid

    # Fall back to name, but never onto a different character. Kenshi names
    # most NPCs by role — Стражник, Житель, Бандит — so a name match alone
    # would hand over someone else's dialogue, stats and relation.
    name = (data.get("target_npc") or "").strip()
    if name:
        strong = _strong_ids_in(*requested)
        for uid in idx.find_by_name(name):
            entity = idx.get(uid)
            if entity is None:
                continue
            if _entity_is_someone_else(entity, strong):
                log.info(
                    f"Name fallback rejected for '{name}': entity '{uid}' holds a "
                    f"different persistent ID"
                )
                continue
            return entity, uid

    return None, None


def _entity_metadata(entity) -> dict:
    meta = {}
    if entity is None:
        return meta
    meta.update({str(k).lstrip("$"): v for k, v in getattr(entity, "fields", {}).items()})
    meta.setdefault("name", getattr(entity, "name", ""))
    meta.setdefault("display_name", getattr(entity, "display_name", ""))
    meta.setdefault("id", getattr(entity, "best_id", ""))
    meta.setdefault("persistent_id", getattr(entity, "persistent_id", ""))
    meta.setdefault("runtime_id", getattr(entity, "runtime_id", ""))
    return meta


def _merge_runtime_metadata(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    def put(k, v):
        if v not in (None, ""):
            merged.setdefault(str(k), v)
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    put(sk, sv)
            else:
                put(k, v)
    return merged


def _token_context(data: dict, *, target_entity=None, player_message: str = "", speakers=None) -> TokenResolverContext:
    target_meta = _merge_runtime_metadata(_entity_metadata(target_entity), data.get("target_metadata") or {})
    target_meta = _merge_runtime_metadata(target_meta, data.get("knower_context") or {})
    player_context = data.get("player_context") if isinstance(data.get("player_context"), dict) else {}
    player_name = str(
        data.get("player_name")
        or data.get("player")
        or player_context.get("name")
        or player_context.get("player_name")
        or ""
    )
    target_shop_stock = data.get("target_shop_stock") or data.get("shop_stock") or []
    if not isinstance(target_shop_stock, list):
        target_shop_stock = [str(target_shop_stock)] if str(target_shop_stock or "").strip() else []
    recent_events = data.get("recent_events") or []
    if not isinstance(recent_events, list):
        recent_events = [str(recent_events)] if str(recent_events or "").strip() else []
    for key in ("race", "persona_category", "mode", "target_npc", "target_npc_id"):
        if data.get(key) not in (None, ""):
            target_meta.setdefault(key, data.get(key))
    return TokenResolverContext(
        campaign_name=cm.active_name or "",
        campaign_root=cm.active_path or "",
        player_message=player_message or data.get("player_message", "") or "",
        player_name=player_name,
        target_entity=target_entity,
        target_metadata=target_meta,
        radiant_speakers=speakers or data.get("speakers") or [],
        nearby_npcs=(data.get("nearby") if isinstance(data.get("nearby"), list) else (target_meta.get("nearby") if isinstance(target_meta.get("nearby"), list) else [])),
        target_shop_stock=target_shop_stock,
        recent_events=[str(e) for e in recent_events if str(e).strip()],
        server_town=str(data.get("server_town") or data.get("town") or ""),
        server_region=str(data.get("server_region") or data.get("region") or ""),
        world_synthesis_path=str(data.get("world_synthesis_path") or ""),
        world_synthesis=str(data.get("world_synthesis") or ""),
        player_context=player_context,
        current_location=str(data.get("current_location") or ""),
        player_status=str(data.get("player_status") or ""),
        player_inventory=str(data.get("player_inventory") or ""),
        mode_guidance=str(data.get("mode_guidance") or ""),
        final_instruction=str(data.get("final_instruction") or ""),
        language_instruction=str(data.get("language_instruction") or ""),
        available_action_tags=str(data.get("available_action_tags") or ""),
        chat_output_contract=str(data.get("chat_output_contract") or ""),
        radiant_output_contract=str(data.get("radiant_output_contract") or ""),
        profile_json_contract=str(data.get("profile_json_contract") or ""),
        campaign_chronicle=str(data.get("campaign_chronicle") or data.get("extra_context") or ""),
    )


def _build_knower_context(target_entity, raw_context):
    context = {}

    if target_entity is not None:
        context.update({k.lstrip("$"): v for k, v in target_entity.fields.items()})
        context.setdefault("name", target_entity.name)
        context.setdefault("display_name", target_entity.display_name)
        if target_entity.entity_id:
            context.setdefault("id", target_entity.entity_id)
        if target_entity.persistent_id:
            context.setdefault("persistent_id", target_entity.persistent_id)
        if target_entity.runtime_id:
            context.setdefault("runtime_id", target_entity.runtime_id)

    def _merge_value(dst: dict, key: str, value):
        if value in (None, ""):
            return
        if isinstance(value, dict):
            existing = dst.get(key)
            nested = dict(existing) if isinstance(existing, dict) else {}
            for subkey, subvalue in value.items():
                _merge_value(nested, str(subkey), subvalue)
            dst[key] = nested
            return
        dst[key] = value

    if isinstance(raw_context, dict):
        for key, value in raw_context.items():
            _merge_value(context, str(key), value)

    for source_key in ("town_name", "town", "location", "location_name"):
        value = context.get(source_key)
        if value and not context.get("city"):
            context["city"] = value
            break
    if context.get("home_region") and not context.get("region"):
        context["region"] = context["home_region"]

    return context


def _upsert_entity_header_line(content: str, key: str, value: str) -> str:
    import re as _re

    pattern = _re.compile(
        rf"^(?P<indent>[ \t]*){_re.escape(key)}[ \t]*=[ \t]*.*$",
        _re.MULTILINE,
    )
    new_line = f"{key} = {value}"
    if pattern.search(content):
        return pattern.sub(lambda m: f"{m.group('indent')}{new_line}", content, count=1)

    if key.lower() == "name":
        category_line = _re.search(r"^[ \t]*Category[ \t]*=.*$", content, _re.MULTILINE)
        if category_line:
            return content[:category_line.end()] + "\n" + new_line + content[category_line.end():]
    return new_line + "\n" + content.lstrip("\n")


def _upsert_entity_field_line(content: str, field: str, value: str) -> str:
    import re as _re

    pattern = _re.compile(
        rf"^(?P<indent>[ \t]*)(?P<key>{_re.escape(field)})[ \t]*=[ \t]*.*$",
        _re.MULTILINE,
    )
    new_line = f"{field} = {value}"
    if pattern.search(content):
        return pattern.sub(lambda m: f"{m.group('indent')}{new_line}", content, count=1)

    prose_start = _re.search(r"^[ \t]*\$\w", content, _re.MULTILINE)
    if prose_start:
        return content[:prose_start.start()] + new_line + "\n" + content[prose_start.start():]
    return content.rstrip() + "\n" + new_line + "\n"


def _extract_entity_field_value(content: str, field: str):
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<key>{re.escape(field)})[ \t]*=[ \t]*(?P<value>.*)$",
        re.MULTILINE,
    )
    match = pattern.search(content or "")
    if not match:
        return None
    return match.group("value").strip()


# Постоянный идентификатор персонажа Kenshi: пять числовых частей через дефис
# (например "1-2522094848-4-127075192-1"). runtime_id живёт одну сессию игры,
# меняется при каждой загрузке и для различения персонажей не годится.
_STRONG_ID_RE = re.compile(r"\d+(?:-\d+){4}")


def _is_strong_entity_id(value) -> bool:
    text = str(value or "").strip()
    if not text or text.isdigit() or text.lower().startswith("hand_"):
        return False
    return bool(_STRONG_ID_RE.fullmatch(text))


def _strong_ids_in(*values) -> set:
    return {str(v).strip() for v in values if _is_strong_entity_id(v)}


def _entity_is_someone_else(entity, requested_strong_ids: set) -> bool:
    """True, если у записи свой постоянный ID и он не тот, который спрашивают."""
    if not requested_strong_ids:
        return False
    own = _strong_ids_in(
        getattr(entity, "persistent_id", ""),
        getattr(entity, "entity_id", ""),
    )
    if not own:
        return False  # запись без ID — к ней можно привязаться
    return own.isdisjoint(requested_strong_ids)


def _entity_ids_in_file(entity_file) -> set:
    try:
        text = entity_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    return _strong_ids_in(
        _extract_entity_field_value(text, "persistent_id"),
        _extract_entity_field_value(text, "Id"),
    )


def _folder_for_npc_write(category: str, name: str, content: str) -> str:
    """Выбрать папку так, чтобы разные NPC с одним именем не слились.

    Возвращает папку уже записанного персонажа, если этот ID известен;
    добавляет короткий суффикс, если имя занято кем-то другим.
    """
    if category not in ("campaign_npcs", "base_npcs") or not cm.active_path:
        return name

    ids = _strong_ids_in(
        _extract_entity_field_value(content, "persistent_id"),
        _extract_entity_field_value(content, "Id"),
    )
    if not ids:
        return name  # различать нечем — прежнее поведение

    # Этот персонаж уже где-то записан: пишем туда же, даже если папку
    # переименовали вслед за игровым именем.
    if cm.indexer:
        for value in sorted(ids):
            uid = cm.indexer.find_by_id(value)
            if uid:
                entity = cm.indexer.get(uid)
                folder = os.path.basename(str(getattr(entity, "path", "") or ""))
                if folder:
                    return folder

    categories_root = pathlib.Path(cm.active_path) / "categories"

    def _occupant(folder: str) -> set:
        return _entity_ids_in_file(categories_root / category / folder / "entity.txt")

    taken = _occupant(name)
    if not taken or not taken.isdisjoint(ids):
        return name  # свободно, без ID, или это наша же запись

    suffix = hashlib.sha1(sorted(ids)[0].encode("utf-8")).hexdigest()[:6]
    for attempt in range(20):
        candidate = f"{name}__{suffix}" if attempt == 0 else f"{name}__{suffix}_{attempt + 1}"
        other = _occupant(candidate)
        if not other or not other.isdisjoint(ids):
            return candidate
    return name


def _merge_unique_npc_fields_on_first_create(category: str, name: str, content: str) -> str:
    if category != "campaign_npcs" or not cm.active_path:
        return content

    try:
        campaign_root = pathlib.Path(cm.active_path)
        categories_root = campaign_root / "categories"
        entity_dir = categories_root / category / name
        entity_file = entity_dir / "entity.txt"
        if entity_file.exists():
            log.info(f"Unique NPC merge skipped: live entity already exists for {category}/{name}")
            return content

        campaign_source = campaign_root / "unique_npcs" / name / "entity.txt"
        template_root = pathlib.Path(cm.template_dir)
        template_source = template_root / "unique_npcs" / name / "entity.txt"
        try:
            _assert_path_within(campaign_root, campaign_source, "unique_npc campaign source")
            _assert_path_within(template_root, template_source, "unique_npc template source")
        except ValueError as e:
            log.warning(f"Unique NPC merge skipped: invalid source path for {category}/{name}: {e}")
            return content

        source_path = None
        source_label = ""
        if campaign_source.exists():
            source_path = campaign_source
            source_label = "campaign"
        elif template_source.exists():
            log.info(
                f"Unique NPC merge fallback: campaign source missing for {category}/{name}; "
                f"using template source"
            )
            source_path = template_source
            source_label = "template"
        else:
            log.info(f"Unique NPC merge skipped: no template found for {category}/{name}")
            return content

        try:
            source_content = source_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning(f"Unique NPC merge skipped: failed to read source for {category}/{name}: {e}")
            return content

        updated = content
        merged_fields = []
        for field in _UNIQUE_NPC_MERGE_FIELDS:
            source_value = _extract_entity_field_value(source_content, field)
            if not source_value:
                continue
            updated = _upsert_entity_field_line(updated, field, source_value)
            merged_fields.append(field)

        if merged_fields:
            merged = ", ".join(merged_fields)
            log.info(
                f"Unique NPC merge applied: {category}/{name} "
                f"source={source_label} fields=[{merged}]"
            )
        else:
            log.info(
                f"Unique NPC merge skipped: source found but no mergeable fields for {category}/{name}"
            )
        return updated
    except Exception as e:
        log.warning(f"Unique NPC merge skipped: unexpected error for {category}/{name}: {e}")
        return content


def _clear_entity_field_values(content: str, fields) -> tuple[str, list[str]]:
    import re as _re

    updated = content
    cleared = []
    seen = set()
    for raw_field in fields or []:
        field = str(raw_field or "").strip()
        if not field or field in seen:
            continue
        seen.add(field)
        pattern = _re.compile(
            rf"^(?P<indent>[ \t]*)(?P<key>{_re.escape(field)})[ \t]*=[ \t]*.*$",
            _re.MULTILINE,
        )
        if not pattern.search(updated):
            continue
        updated = pattern.sub(lambda m: f"{m.group('indent')}{m.group('key')} = ", updated)
        cleared.append(field)
    return updated, cleared


def _log_prompt(campaign_root: str, prompt_type: str, prompt: str, keep: int = 20):
    """Log a raw prompt to logs/<type>.log, keeping last `keep` entries."""
    try:
        import pathlib
        log_dir = pathlib.Path(campaign_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{prompt_type.lower()}.log"
        DIV = "\n" + "=" * 80 + "\n"
        existing = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ""
        entries = [e for e in existing.split(DIV) if e.strip()]
        entries.append(prompt.strip())
        if len(entries) > keep:
            entries = entries[-keep:]
        log_path.write_text(DIV.join(entries), encoding='utf-8')
    except Exception:
        pass

def _log_perf(campaign_root: str, retrieval_meta: dict, prompt_type: str,
              keywords: list, target: str = None, keep: int = 200):
    """
    Append one performance record to logs/retrieval_perf.log.
    Each line is a JSON object — easy to grep, diff, or analyse.

    Fields logged:
      ts          — ISO timestamp
      prompt_type — chat / loremaster / biography / speak
      target      — NPC name or None
      keywords    — keywords used
      elapsed_ms  — how long retrieval took
      timeout_hit — True if deadline fired before retrieval finished
      entities    — number of entities returned
      cfg         — config snapshot (max_layers, timeout_ms, etc.)
    """
    try:
        import pathlib, json as _json
        from datetime import datetime as _dt
        log_dir = pathlib.Path(campaign_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "retrieval_perf.log"
        record = {
            "ts":          _dt.now().isoformat(timespec='seconds'),
            "prompt_type": prompt_type,
            "target":      target,
            "keywords":    keywords,
            "elapsed_ms":  retrieval_meta.get("elapsed_ms"),
            "timeout_hit": retrieval_meta.get("timeout_hit", False),
            "entities":    retrieval_meta.get("entities_found"),
            "cfg":         retrieval_meta.get("config_snapshot", {}),
        }
        # Read existing, trim to keep, append
        lines = []
        if log_path.exists():
            lines = [l for l in log_path.read_text(encoding='utf-8').splitlines() if l.strip()]
        lines.append(_json.dumps(record))
        if len(lines) > keep:
            lines = lines[-keep:]
        log_path.write_text("\n".join(lines) + "\n", encoding='utf-8')
        if record["timeout_hit"]:
            log.warning(
                f"Retrieval timeout hit: {prompt_type} target={target} "
                f"elapsed={record['elapsed_ms']}ms "
                f"cfg=layers:{record['cfg'].get('max_layers')} "
                f"files:{record['cfg'].get('max_files')} "
                f"timeout:{record['cfg'].get('timeout_ms')}ms"
            )
    except Exception:
        pass  # perf logging must never break the request pipeline


# ─── STATUS ──────────────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    idx_stats  = cm.indexer.stats() if cm.indexer else {}
    safe_cfg   = {k: (list(v) if isinstance(v, frozenset) else v) for k, v in cm.config.items()}
    return jsonify({"status": "ok", "campaign": cm.active_name, "index": idx_stats, "config": safe_cfg})

# ─── CAMPAIGN ────────────────────────────────────────────────────────────────

@app.route("/campaign/list", methods=["GET"])
def list_campaigns():
    return jsonify({"campaigns": cm.list_campaigns()})

@app.route("/campaign/create", methods=["POST"])
def create_campaign():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required"}), 400
    try:
        path = cm.create_campaign(name)
        log.info(f"Campaign created: {name}")
        return jsonify({"status": "ok", "campaign": name, "path": path})
    except ValueError as e:
        msg = str(e)
        status = 409 if "already exists" in msg.lower() else 400
        return jsonify({"error": msg}), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/campaign/load", methods=["POST"])
def load_campaign():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required"}), 400
    try:
        with _lock:
            cm.load_campaign(name)
        log.info(f"Campaign loaded: {name}  {cm.indexer.stats()}")
        return jsonify({"status": "ok", "campaign": name, "index": cm.indexer.stats()})
    except ValueError as e:
        msg = str(e)
        status = 404 if "does not exist" in msg.lower() else 400
        return jsonify({"error": msg}), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/campaign/switch", methods=["POST"])
def switch_campaign():
    """Create-if-needed then load. The one-shot endpoint for save events."""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required"}), 400
    try:
        with _lock:
            created = False
            if not cm.campaign_exists(name):
                cm.create_campaign(name)
                created = True
                log.info(f"Campaign created from Template: {name}")
            cm.load_campaign(name)
        log.info(f"Campaign switched to '{name}' (created={created})  {cm.indexer.stats()}")
        return jsonify({"status": "ok", "campaign": name, "created": created, "index": cm.indexer.stats()})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.exception("switch_campaign")
        return jsonify({"error": str(e)}), 500

@app.route("/campaign/reload_index", methods=["POST"])
def reload_index():
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err
    with _lock:
        cm.reload_index()
    log.info(f"Index reloaded: {cm.active_name}  {cm.indexer.stats()}")
    return jsonify({"status": "ok", "index": cm.indexer.stats()})

@app.route("/config/set", methods=["POST"])
def config_set():
    """
    Set a live config value and persist it to core_config.txt.

    Bridges use this to adjust global economy, retrieval tuning, or any
    other config key at runtime without restarting the server.

    Request body:
        key   : str          — config key (e.g. "price_modifier")
        value : str|float    — new value

    Example:
        {"key": "price_modifier", "value": 1.25}
    """
    data  = request.get_json(force=True, silent=True) or {}
    key   = (data.get("key")   or "").strip()
    value =  data.get("value")

    if not key or value is None:
        return jsonify({"error": "'key' and 'value' are required"}), 400

    import pathlib, re as _re

    # Apply to live config
    with _lock:
        cm.config[key] = value
        # Re-coerce floats
        from scripts.config import _FLOAT_KEYS, _INT_KEYS, DEFAULTS
        if key in _FLOAT_KEYS:
            try:
                cm.config[key] = float(value)
            except (ValueError, TypeError):
                cm.config[key] = DEFAULTS.get(key, value)
        elif key in _INT_KEYS:
            try:
                cm.config[key] = int(value)
            except (ValueError, TypeError):
                cm.config[key] = DEFAULTS.get(key, value)

    # Persist to core_config.txt
    cfg_path = pathlib.Path(cm.config_dir) / "core_config.txt"
    try:
        text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        pattern = _re.compile(
            rf"^(?P<indent>[ \t]*)(?P<key>{_re.escape(key)})[ \t]*=[ \t]*.*$",
            _re.MULTILINE,
        )
        new_line = f"{key} = {value}"
        if pattern.search(text):
            text = pattern.sub(new_line, text)
        else:
            text = text.rstrip() + f"\n{new_line}\n"
        cfg_path.write_text(text, encoding="utf-8")
        log.info(f"Config set: {key} = {value} (persisted)")
    except Exception as e:
        log.warning(f"Config set: could not persist {key} = {value}: {e}")

    return jsonify({"status": "ok", "key": key, "value": cm.config.get(key)})


@app.route("/config/get", methods=["GET", "POST"])
def config_get():
    """
    Return current config values.
    Pass {"key": "price_modifier"} to get a single value, or omit for all.
    """
    data = request.get_json(force=True, silent=True) or {}
    key  = (data.get("key") or "").strip()
    safe = {k: (list(v) if isinstance(v, frozenset) else v)
            for k, v in cm.config.items()}
    if key:
        if key not in safe:
            return jsonify({"error": f"unknown key: {key}"}), 404
        return jsonify({"key": key, "value": safe[key]})
    return jsonify({"config": safe})


@app.route("/config/reload", methods=["POST"])
def config_reload():
    with _lock:
        new_cfg = cm.reload_config()
    safe = {k: (list(v) if isinstance(v, frozenset) else v) for k, v in new_cfg.items()}
    log.info(f"Config reloaded")
    return jsonify({"status": "ok", "config": safe})

# ─── WRITE ENDPOINTS ─────────────────────────────────────────────────────────

@app.route("/write/mandatory", methods=["POST"])
def write_mandatory():
    """
    Write a per-campaign mandatory prompt file.

    Used by the SS bridge to sync the player bio (5_player_bio.txt) into
    Campaigns/<name>/mandatory/Chat/ on every save load, so the LLM always
    knows who the player character is.

    Request body:
        filename    : str  — e.g. "5_player_bio.txt"
        content     : str  — full text content to write
        scope       : str  — "campaign" (default) or "template"
        prompt_type : str  — subdirectory under mandatory/ e.g. "Chat"
        campaign    : str  — campaign name (required when scope="campaign")
    """
    data = request.get_json(force=True, silent=True) or {}
    filename    = (data.get("filename")    or "").strip()
    content     = data.get("content", "")
    scope       = (data.get("scope")       or "campaign").strip()
    prompt_type = (data.get("prompt_type") or "Chat").strip()
    campaign    = (data.get("campaign")    or "").strip()

    try:
        filename = _safe_segment(filename, "filename")
        prompt_type = _safe_segment(prompt_type, "prompt_type")
        if scope not in ("campaign", "template"):
            return jsonify({"error": "'scope' must be 'campaign' or 'template'"}), 400

        if scope == "template":
            base = pathlib.Path(cm.db_root) / "Template" / "mandatory" / prompt_type
        else:
            if not campaign:
                return jsonify({"error": "'campaign' is required for scope=campaign"}), 400
            campaign = _safe_segment(campaign, "campaign")
            base = pathlib.Path(cm.db_root) / "Campaigns" / campaign / "mandatory" / prompt_type

        base.mkdir(parents=True, exist_ok=True)
        dest = base / filename
        _assert_path_within(base, dest, "filename")
        dest.write_text(content, encoding="utf-8")
        log.info(f"Mandatory file written: {dest}")
        return jsonify({"status": "ok", "path": str(dest)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.exception("write_mandatory")
        return jsonify({"error": str(e)}), 500


@app.route("/write/npc", methods=["POST"])
def write_npc():
    """
    Write or update an entity.txt for an NPC and incrementally reindex it.

    The bridge sends pre-formatted entity.txt content. Kayak writes it to
    the correct path and updates the index without a full rebuild.

    Request body:
        category       : str  — "campaign_npcs" or "base_npcs"
        name           : str  — folder-safe NPC name (e.g. "Paladin_Tealc")
        entity_content : str  — full content of entity.txt
        campaign       : str  — optional campaign switch
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    category = (data.get("category") or "campaign_npcs").strip()
    name     = (data.get("name")     or "").strip()
    content  = (data.get("entity_content") or "").strip()

    if not content:
        return jsonify({"error": "name and entity_content are required"}), 400

    try:
        category = _safe_segment(category, "category")
        name = _safe_segment(name, "name")
        resolved_name = _folder_for_npc_write(category, name, content)
        if resolved_name != name:
            log.info(
                f"NPC folder disambiguated: {category}/{name} is taken by another "
                f"character -> {resolved_name}"
            )
            content = _upsert_entity_field_line(content, "Name", resolved_name)
            name = resolved_name
        content = _merge_unique_npc_fields_on_first_create(category, name, content)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    categories_root = pathlib.Path(cm.active_path) / "categories"
    entity_dir = categories_root / category / name
    try:
        _assert_path_within(categories_root, entity_dir, "name/category")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    entity_dir.mkdir(parents=True, exist_ok=True)
    entity_file = entity_dir / "entity.txt"
    entity_file.write_text(content, encoding="utf-8")

    # Incremental reindex — only this entity, no full rebuild
    uid = cm.indexer.add_or_update_entity(str(entity_dir))
    log.info(f"NPC written + indexed: {category}/{name} uid={uid}")
    return jsonify({"status": "ok", "uid": uid, "path": str(entity_dir)})


@app.route("/rename/npc", methods=["POST"])
def rename_npc():
    """
    Rename an NPC entity folder and sync the canonical name fields.

    This is intended as a post-write follow-up for in-game renames:
      1. Resolve the entity by persistent/runtime ID when available
      2. Move the entity folder if the slug changed
      3. Update header Name plus display_name inside entity.txt
      4. Reindex only the affected entity

    Request body:
        old_name          : str  — current folder/name hint
        new_name          : str  — new folder-safe entity name
        new_display_name  : str  — new human-readable display name
        id                : str  — alias for target_npc_id
        target_npc_id     : str  — preferred persistent/runtime ID
        persistent_id     : str  — alias
        runtime_id        : str  — alias
        campaign          : str  — optional campaign switch
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    old_name = str(
        data.get("old_name")
        or data.get("target_npc")
        or ""
    ).strip()
    new_name_raw = str(data.get("new_name") or "").strip()
    new_display_name = str(
        data.get("new_display_name")
        or data.get("display_name")
        or new_name_raw.replace("_", " ")
    ).strip()

    if "\x00" in new_display_name:
        return jsonify({"error": "Invalid new_display_name"}), 400
    new_display_name = new_display_name.replace("\r", " ").replace("\n", " ")

    try:
        new_name = _safe_segment(new_name_raw, "new_name")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    lookup_data = dict(data)
    if old_name and not lookup_data.get("target_npc"):
        lookup_data["target_npc"] = old_name
    if data.get("id") and not lookup_data.get("target_npc_id"):
        lookup_data["target_npc_id"] = str(data.get("id")).strip()

    categories_root = pathlib.Path(cm.active_path) / "categories"

    with _lock:
        target_entity, target_uid = _resolve_target(lookup_data)
        if target_entity is None:
            return jsonify({"error": "Entity not found"}), 404

        source_dir = pathlib.Path(target_entity.path)
        source_file = source_dir / "entity.txt"
        if not source_file.exists():
            return jsonify({"error": "Entity file not found"}), 404

        dest_dir = source_dir.parent / new_name
        try:
            _assert_path_within(categories_root, source_dir, "source")
            _assert_path_within(categories_root, dest_dir, "new_name")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        same_dir = (
            os.path.normcase(str(source_dir.resolve(strict=False)))
            == os.path.normcase(str(dest_dir.resolve(strict=False)))
        )
        if not same_dir and dest_dir.exists():
            return jsonify({"error": f"Destination '{new_name}' already exists"}), 409

        original_content = source_file.read_text(encoding="utf-8")
        updated_content = _upsert_entity_header_line(original_content, "Name", new_name)
        updated_content = _upsert_entity_field_line(updated_content, "display_name", new_display_name)

        moved = False
        try:
            if not same_dir:
                source_dir.rename(dest_dir)
                moved = True

            active_dir = dest_dir if moved else source_dir
            active_file = active_dir / "entity.txt"
            active_file.write_text(updated_content, encoding="utf-8")

            if target_uid:
                cm.indexer._remove_entity(target_uid)
            new_uid = cm.indexer.add_or_update_entity(str(active_dir))
        except Exception:
            try:
                if moved and dest_dir.exists() and not source_dir.exists():
                    dest_dir.rename(source_dir)
                source_file.write_text(original_content, encoding="utf-8")
                cm.indexer.add_or_update_entity(str(source_dir))
            except Exception:
                log.exception("rename_npc rollback")
            raise

    log.info(
        f"NPC renamed + indexed: {target_entity.category}/{source_dir.name} -> "
        f"{target_entity.category}/{new_name} ({new_display_name})"
    )
    return jsonify({
        "status": "ok",
        "old_name": source_dir.name,
        "new_name": new_name,
        "display_name": new_display_name,
        "uid": new_uid,
        "moved": not same_dir,
    })


@app.route("/write/stats", methods=["POST"])
def write_stats():
    """
    Write stats.txt for an entity.
    No reindex — stats.txt is read fresh at prompt-time.

    Request body:
        target_npc     : str  — NPC name
        target_npc_id  : str  — persistent or runtime ID (preferred)
        persistent_id  : str  — alias for target_npc_id
        runtime_id     : str  — alias for target_npc_id
        stats_content  : str  — content to write
        campaign       : str  — optional
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    entity, _ = _resolve_target(data)
    if not entity:
        return jsonify({"error": "Entity not found"}), 404

    content = (data.get("stats_content") or "").strip()
    if not content:
        return jsonify({"error": "stats_content is required"}), 400

    cm.prompt_builder.update_stats(entity, content)
    return jsonify({"status": "ok", "entity": entity.name})


@app.route("/write/world_event", methods=["POST"])
def write_world_event():
    """
    Write a world event entity into categories/world_events/.
    Incrementally indexed.

    Request body:
        name         : str  — event identifier (e.g. "siege_of_stack_day57")
        entity_content : str
        campaign     : str  — optional
    """
    data    = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    name    = (data.get("name")           or "").strip()
    content = (data.get("entity_content") or "").strip()
    if not content:
        return jsonify({"error": "name and entity_content are required"}), 400

    try:
        name = _safe_segment(name, "name")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    categories_root = pathlib.Path(cm.active_path) / "categories"
    entity_dir = categories_root / "world_events" / name
    try:
        _assert_path_within(categories_root, entity_dir, "name")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "entity.txt").write_text(content, encoding="utf-8")

    uid = cm.indexer.add_or_update_entity(str(entity_dir))
    return jsonify({"status": "ok", "uid": uid})


@app.route("/write/lore_chunk", methods=["POST"])
def write_lore_chunk():
    """
    Write a single lore chunk entity (from World_lore.json import).

    Request body:
        lore_type      : str  — faction/race/theology/region/global
        name           : str  — chunk id (e.g. "lore_holy_nation_overview")
        entity_content : str
        campaign       : str  — optional
    """
    data      = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    lore_type = (data.get("lore_type") or "global").strip()
    name      = (data.get("name")      or "").strip()
    content   = (data.get("entity_content") or "").strip()
    if not content:
        return jsonify({"error": "name and entity_content are required"}), 400

    try:
        lore_type = _safe_segment(lore_type, "lore_type")
        name = _safe_segment(name, "name")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    categories_root = pathlib.Path(cm.active_path) / "categories"
    entity_dir = categories_root / f"lore_{lore_type}" / name
    try:
        _assert_path_within(categories_root, entity_dir, "name/lore_type")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "entity.txt").write_text(content, encoding="utf-8")

    uid = cm.indexer.add_or_update_entity(str(entity_dir))
    return jsonify({"status": "ok", "uid": uid, "lore_type": lore_type})


# ─── PROMPT / CHAT ───────────────────────────────────────────────────────────

@app.route("/prompt/chat", methods=["POST"])
def prompt_chat():
    """
    Build a chat prompt.

    Request body:
        player_message : str
        target_npc     : str   — NPC folder name
        target_npc_id  : str   — any known ID (persistent preferred)
        persistent_id  : str   — alias
        runtime_id     : str   — alias
        stats_content  : str   — live stats block (written to stats.txt)
        mode           : str   — "talk" | "whisper" | "yell"  (default: talk)
        keywords       : list  — override auto-extracted keywords
        campaign       : str   — optional switch
        extra_context     : str        — optional extra text appended to player message
        knowledge_filter  : list[str]  — optional whitelist of entity names; if present,
                                         world context is limited to matching entities
        knower_context    : dict       — optional merged NPC/runtime context for knowledge checks
        disable_knowledge_rules : bool — when true, bypass whitelist + who_knows_me access checks
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    player_message = data.get("player_message", "")
    stats_str      = (data.get("stats_content") or "").strip()
    mode           = (data.get("mode") or "talk").lower().strip()
    extra_ctx      = (data.get("extra_context") or "").strip()
    persona_category = (data.get("persona_category") or "").strip().lower()
    species_race     = (data.get("race") or "").strip()

    # Select policy based on mode
    policy_map = {
        "whisper": POLICY_CHAT_WHISPER,
        "yell":    POLICY_CHAT_YELL,
        "talk":    POLICY_CHAT,
    }
    policy = policy_map.get(mode, POLICY_CHAT)
    if persona_category == "animal":
        from dataclasses import replace
        policy = replace(
            POLICY_CHAT_ANIMAL,
            player_message_suffix=policy.player_message_suffix,
        )
    elif persona_category == "machine":
        from dataclasses import replace
        policy = replace(
            POLICY_CHAT_MACHINE,
            player_message_suffix=policy.player_message_suffix,
        )
    elif persona_category == "feral":
        from dataclasses import replace
        policy = replace(
            POLICY_CHAT_FERAL,
            player_message_suffix=policy.player_message_suffix,
        )
    elif persona_category == "sapient":
        from dataclasses import replace
        policy = replace(
            POLICY_CHAT_SAPIENT,
            player_message_suffix=policy.player_message_suffix,
        )

    from dataclasses import replace
    _prompt_dialogue_keep = int(cm.config.get("prompt_dialogue_keep_lines", policy.dialogue_keep_lines))
    _yell_dialogue_keep = int(cm.config.get("yell_dialogue_keep_lines", _prompt_dialogue_keep))
    _effective_keep = _yell_dialogue_keep if mode == "yell" else _prompt_dialogue_keep
    if _effective_keep < 0:
        _effective_keep = policy.dialogue_keep_lines
    policy = replace(policy, dialogue_keep_lines=_effective_keep)

    # Token prompt architecture: bridge-supplied extra_context is metadata, not
    # an invisible PLAYER_MESSAGE suffix. Prompt files can expose it later with
    # a dedicated token if desired.

    indexer = cm.indexer
    pb      = cm.prompt_builder
    
    # Defensive check: pb should always exist if campaign is loaded (enforced by _need_campaign)
    if not pb:
        return jsonify({"error": "Campaign not properly loaded"}), 503

    # Resolve target entity (persistent_id > runtime_id > name)
    target_entity, target_uid = _resolve_target(data)
    knower_context = _build_knower_context(target_entity, data.get("knower_context"))
    disable_knowledge_rules = bool(data.get("disable_knowledge_rules", False))
    token_ctx = _token_context(data, target_entity=target_entity, player_message=player_message)
    token_resolver = TokenResolver(indexer=indexer, prompt_builder=pb)
    # Сущности, которые промпт покажет и без world_context: текущий город с
    # регионом плюс раса, фракция и родной город самой цели. Иначе NPC читает
    # своё описание дважды.
    already_shown_uids = set()
    try:
        already_shown_uids = token_resolver.current_location_entity_uids(token_ctx)
    except Exception:
        already_shown_uids = set()
    try:
        already_shown_uids |= token_resolver.target_profile_entity_uids(token_ctx)
    except Exception:
        pass

    # Update live stats
    if target_entity and stats_str:
        pb.update_stats(target_entity, stats_str)

    # Extract keywords
    override = data.get("keywords")
    if isinstance(override, list) and override:
        keywords = [str(k) for k in override]
    else:
        keywords = extract_keywords(
            player_message, indexer, cm.config.get("max_keywords", 5)
        )
    keywords = token_resolver.filter_current_location_keywords(token_ctx, keywords)

    # Retrieve world context
    priority   = [target_uid] if target_uid else None
    retriever  = _get_retriever()
    all_ents, _perf_meta = retriever.retrieve(
        keywords,
        priority_uids=priority,
        knower_context=knower_context,
        disable_knowledge_rules=disable_knowledge_rules,
        exempt_uids=priority,
    )
    # Filter out target NPC from world context (only if target_uid is set)
    world_ents = [
        e for e in all_ents
        if (target_uid is None or e.uid != target_uid)
        and e.uid not in already_shown_uids
    ]
    if cm.active_path:
        _log_perf(cm.active_path, _perf_meta, "chat",
                  keywords, target=target_entity.name if target_entity else None)

    raw_filter = data.get("knowledge_filter")
    if (
        not disable_knowledge_rules
        and isinstance(raw_filter, list)
        and raw_filter
        and indexer
    ):
        allowed_uids = []
        allowed_set = set()
        for item in raw_filter:
            token = str(item or "").strip()
            if not token:
                continue
            for uid in indexer.find_by_name(token):
                if uid == target_uid or uid in already_shown_uids or uid in allowed_set:
                    continue
                allowed_set.add(uid)
                allowed_uids.append(uid)

        if allowed_set:
            filtered = [e for e in world_ents if e.uid in allowed_set]
            if not filtered:
                filtered = [indexer.get(uid) for uid in allowed_uids]
                filtered = [e for e in filtered if e is not None and e.uid not in already_shown_uids]
            world_ents = filtered[: cm.config.get("max_files", 5)]

    # Assemble
    prompt = pb.assemble(
        policy         = policy,
        world_entities = world_ents,
        target_entity  = target_entity,
        player_message = player_message,
        species_race   = species_race,
        species_category = persona_category,
        token_context  = token_ctx,
        indexer        = indexer,
    )

    log.debug(
        f"chat | mode={mode} target={target_entity.name if target_entity else None} "
        f"world={len(world_ents)} kw={keywords}"
    )

    return jsonify({
        "prompt":              prompt,
        "target":              target_entity.name if target_entity else None,
        "world_context_count": len(world_ents),
        "keywords_used":       keywords,
        "mode":                mode,
        "prompt_type":         policy.prompt_type,
    })


# ─── PROMPT / LOREMASTER ─────────────────────────────────────────────────────

@app.route("/prompt/speak", methods=["POST"])
def prompt_speak():
    """
    Build a speak prompt — the LLM will repeat the given line verbatim.
    Used by /k_speak so the NPC produces a speech bubble with exact text,
    without any world context, dialogue history, or normal chat scaffolding.

    Request body:
        line       : str  — the exact line the NPC should say
        target_npc : str  — NPC folder name (for clarity in the instruction)
        campaign   : str  — optional switch
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err
    line       = (data.get("line")       or "").strip()
    target_npc = (data.get("target_npc") or "").strip()
    if not line:
        return jsonify({"error": "'line' is required"}), 400
    pb = cm.prompt_builder
    # Load Speak mandatory block (1_speak_rule.txt) then append NPC + line
    sections = []
    token_ctx = _token_context(data, player_message=line)
    for part in pb.load_mandatory("Speak"):
        sections.append(pb.expand_prompt_text(part, token_ctx, cm.indexer))
    if target_npc:
        sections.append(f"--- TARGET_NPC\nSpeaker: {target_npc}")
    sections.append(f"--- LINE\n{line}")
    prompt = "\n\n".join(sections)
    if cm.active_path:
        _log_prompt(cm.active_path, "speak", prompt)
    return jsonify({"prompt": prompt, "mode": "speak", "target": target_npc})


@app.route("/prompt/loremaster", methods=["POST"])
def prompt_loremaster():
    """
    Build a loremaster / narrator prompt.

    Request body:
        events   : str or list
        campaign : str — optional
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    events = data.get("events", "")
    if isinstance(events, list):
        events = "\n".join(str(e) for e in events)

    pb       = cm.prompt_builder
    sections = []
    token_ctx = _token_context(data)
    for content in pb.load_mandatory("Loremaster"):
        sections.append(pb.expand_prompt_text(content, token_ctx, cm.indexer))
    if events:
        sections.append(f"--- EVENTS\n{events.strip()}")
    lore_prompt = "\n\n".join(sections)
    if cm.active_path:
        _log_prompt(cm.active_path, "loremaster", lore_prompt)
    return jsonify({"prompt": lore_prompt})


@app.route("/prompt/radiant", methods=["POST"])
def prompt_radiant():
    """
    Build a category-aware radiant/group banter prompt.

    Request body:
        speakers        : list[dict]
        player_name     : str
        world_lore      : str
        events          : str
        recent_dialogue : str
        campaign        : str — optional
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    speakers = data.get("speakers") or []
    if not isinstance(speakers, list):
        return jsonify({"error": "'speakers' must be a list"}), 400

    pb = cm.prompt_builder
    token_ctx = _token_context(data, speakers=speakers)
    prompt = pb.assemble_radiant(
        speakers=speakers,
        player_name=str(data.get("player_name") or "Drifter"),
        world_lore=str(data.get("world_lore") or "").strip(),
        events=str(data.get("events") or "").strip(),
        recent_dialogue=str(data.get("recent_dialogue") or "").strip(),
        token_context=token_ctx,
        indexer=cm.indexer,
    )
    return jsonify({"prompt": prompt, "prompt_type": POLICY_CHAT_RADIANT.prompt_type})


# ─── PROMPT / BIOGRAPHY ──────────────────────────────────────────────────────

@app.route("/prompt/biography", methods=["POST"])
def prompt_biography():
    """
    Build a biography generation prompt.

    Request body:
        npc_data : str
        campaign : str — optional
    """
    data     = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    npc_data = data.get("npc_data", "")
    persona_category = (data.get("persona_category") or "").strip().lower()
    species_race     = (data.get("race") or "").strip()
    pb       = cm.prompt_builder
    sections = []
    prompt_type = "Biography"
    if persona_category == "animal":
        prompt_type = "BiographyAnimal"
    elif persona_category == "machine":
        prompt_type = "BiographyMachine"
    elif persona_category == "feral":
        prompt_type = "BiographyFeral"
    elif persona_category == "sapient":
        prompt_type = POLICY_BIOGRAPHY_SAPIENT.prompt_type
    token_ctx = _token_context(data, player_message=npc_data)
    for content in pb.load_mandatory(prompt_type):
        sections.append(pb.expand_prompt_text(content, token_ctx, cm.indexer))
    for content in pb.load_species_overlays(prompt_type, species_race, persona_category):
        sections.append(pb.expand_prompt_text(content, token_ctx, cm.indexer))
    if npc_data:
        sections.append(f"--- NPC_DATA\n{npc_data.strip()}")
    bio_prompt = "\n\n".join(sections)
    if cm.active_path:
        _log_prompt(cm.active_path, "biography", bio_prompt)
    return jsonify({"prompt": bio_prompt, "prompt_type": prompt_type})


# ─── DIALOGUE PERSISTENCE ────────────────────────────────────────────────────

@app.route("/write/npc_knowledge", methods=["POST"])
def write_npc_knowledge():
    """
    Atomically add one location/entity name to an NPC's $knows_about field.
    Used by the bridge when a dialogue occurs at a new location — the NPC
    "remembers" that place and can reference it in future conversations.

    Creates the NPC entity if it doesn't exist yet (no-op on missing NPCs
    without a $knows_about field, so the endpoint is always safe to call).

    Request body:
        target_npc   : str  — NPC folder name (e.g. "Paladin_Tealc")
        location     : str  — entity name to add (e.g. "Heft")
        campaign     : str  — campaign name
    """
    data     = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    npc_name  = (data.get("target_npc") or "").strip()
    location  = (data.get("location")   or "").strip()

    if not npc_name or not location:
        return jsonify({"error": "target_npc and location are required"}), 400

    import re as _re

    target_entity, _ = _resolve_target(data)
    if target_entity is None:
        # NPC not found — silently ignore (profile may not have been written yet)
        return jsonify({"status": "noop", "reason": "entity not found"})

    entity_path = pathlib.Path(target_entity.path) / "entity.txt"
    if not entity_path.exists():
        return jsonify({"status": "noop", "reason": "entity file missing"})

    try:
        content = entity_path.read_text(encoding="utf-8")

        # Normalised location for comparison
        def _n(s): return s.strip().replace(" ", "_").lower()
        loc_norm = _n(location)

        # Verify the location resolves to a known KayakDB entity (avoid pollution)
        known_uids = cm.indexer.find_by_name(location)
        if not known_uids:
            return jsonify({"status": "noop", "reason": f"'{location}' not in index"})

        # Parse existing knows_about
        ka_match = _re.search(r'^\$knows_about\s*=\s*(.*)$', content, _re.MULTILINE)
        if ka_match:
            existing_raw = ka_match.group(1)
            existing = [p.strip() for p in existing_raw.split(",") if p.strip()]
            existing_norm = {_n(x) for x in existing}
            if loc_norm in existing_norm:
                return jsonify({"status": "noop", "reason": "already known"})
            existing.append(location)
            new_line = f"$knows_about = {', '.join(existing)}"
            content = content[:ka_match.start()] + new_line + content[ka_match.end():]
        else:
            # No $knows_about yet — append it before the first $-prose field or at end
            insert_before = _re.search(r'^\$\w', content, _re.MULTILINE)
            new_line = f"$knows_about = {location}\n"
            if insert_before:
                pos = insert_before.start()
                content = content[:pos] + new_line + content[pos:]
            else:
                content = content.rstrip() + "\n" + new_line

        entity_path.write_text(content, encoding="utf-8")
        log.info(f"NPC knowledge: {target_entity.name} now knows '{location}'")
        return jsonify({"status": "ok", "added": location})

    except Exception as e:
        log.exception("write_npc_knowledge")
        return jsonify({"error": str(e)}), 500


@app.route("/entity/fields", methods=["POST"])
def entity_fields():
    """
    Return the fields of a named entity.

    Bridges use this to read per-NPC data (e.g. $knows_about) without
    needing direct filesystem access.  Returns {} if the entity is not found.

    Request body:
        target_npc : str  — folder name
        campaign   : str  — optional switch
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    target_entity, _ = _resolve_target(data)
    if target_entity is None:
        return jsonify({"fields": {}, "found": False})

    # Return fields with $ stripped (same convention as render_entity)
    clean_fields = {k.lstrip("$"): v for k, v in target_entity.fields.items()}
    clean_fields.setdefault("category", target_entity.category or "")
    clean_fields.setdefault("name", target_entity.name or "")
    clean_fields.setdefault("display_name", target_entity.display_name or "")
    clean_fields.setdefault("id", target_entity.entity_id or "")
    clean_fields.setdefault("persistent_id", target_entity.persistent_id or "")
    clean_fields.setdefault("runtime_id", target_entity.runtime_id or "")
    return jsonify({
        "found":    True,
        "name":     target_entity.name,
        "category": target_entity.category,
        "fields":   clean_fields,
    })


@app.route("/entity/all_npcs", methods=["POST"])
def entity_all_npcs():
    """
    Return all NPC entities as a name -> fields mapping.
    Used by SentientSands' in-memory character registry on campaign load.
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    npcs = {}
    for entity in cm.indexer.entities.values():
        if entity.category not in {"campaign_npcs", "base_npcs"}:
            continue
        fields = {k.lstrip("$"): v for k, v in entity.fields.items()}
        fields.setdefault("category", entity.category)
        fields.setdefault("_entity_category", entity.category)
        fields.setdefault("display_name", entity.display_name)
        fields.setdefault("id", entity.entity_id or "")
        fields.setdefault("persistent_id", entity.persistent_id or "")
        fields.setdefault("runtime_id", entity.runtime_id or "")
        fields.setdefault("_entity_name", entity.name)
        fields["mtime"] = getattr(entity, "mtime", 0.0)
        key = entity.display_name
        if key in npcs:
            suffix = entity.best_id or entity.name
            key = f"{key}__{suffix}"
        npcs[key] = fields

    return jsonify({"status": "ok", "count": len(npcs), "npcs": npcs})


@app.route("/write/entity_field", methods=["POST"])
def write_entity_field():
    """
    Set or update a single field on an existing entity without rewriting the whole file.
    Used by bridges to patch individual fields (e.g. price_modifier on a city entity).
    Triggers an incremental reindex for the changed entity.

    Request body:
        name     : str  — entity folder name (e.g. "Heft")
        field    : str  — field key to set (e.g. "price_modifier")
        value    : str  — new value as string
        campaign : str  — optional campaign
    """
    data  = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    name  = (data.get("name") or data.get("target_npc") or "").strip()
    field = (data.get("field") or "").strip()
    value = str(data.get("value", "")).strip()

    if not field:
        return jsonify({"error": "'field' is required"}), 400
    if not name and not any((data.get("target_npc_id"), data.get("persistent_id"), data.get("runtime_id"))):
        return jsonify({"error": "'name' or an ID is required"}), 400

    import pathlib, re as _re

    try:
        field = _safe_field_key(field)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if "\x00" in value:
        return jsonify({"error": "Invalid value"}), 400
    value = value.replace("\r", " ").replace("\n", " ")

    lookup_data = dict(data)
    if name and not lookup_data.get("target_npc"):
        lookup_data["target_npc"] = name
    target_entity, _ = _resolve_target(lookup_data)

    entity_path = None
    if target_entity is not None:
        entity_path = pathlib.Path(target_entity.path) / "entity.txt"
        if not entity_path.exists():
            entity_path = None

    # Fallback for clients that only pass "name" and no IDs.
    if entity_path is None and name:
        categories_root = pathlib.Path(cm.active_path) / "categories"
        matches = []
        wanted = name.lower()
        for p in categories_root.rglob("entity.txt"):
            if p.parent.name.lower() == wanted:
                matches.append(p)
        if matches:
            matches.sort(key=lambda p: (
                0 if p.parent.name == name else 1,
                0 if "campaign_npcs" in {part.lower() for part in p.parts} else 1,
                len(p.parts),
            ))
            entity_path = matches[0]

    if entity_path is None:
        return jsonify({"error": f"entity '{name}' not found"}), 404

    resolved_name = name
    try:
        campaign_root = pathlib.Path(cm.active_path)
        _assert_path_within(campaign_root, entity_path, "entity")
        content = entity_path.read_text(encoding="utf-8")
        if field.lower() == "id":
            pattern = _re.compile(r"^(?P<indent>[ \t]*)Id[ \t]*=[ \t]*.*$", _re.MULTILINE)
            new_line = f"Id = {value}"
            if pattern.search(content):
                content = pattern.sub(lambda m: f"{m.group('indent')}{new_line}", content, count=1)
            else:
                m = _re.search(r"^[ \t]*Name[ \t]*=.*$", content, _re.MULTILINE)
                if m:
                    content = content[:m.end()] + "\n" + new_line + content[m.end():]
                else:
                    content = new_line + "\n" + content.lstrip("\n")
        else:
            pattern = _re.compile(
                rf"^(?P<indent>[ \t]*)(?P<key>{_re.escape(field)})[ \t]*=[ \t]*.*$",
                _re.MULTILINE,
            )
            new_line = f"{field} = {value}"
            if pattern.search(content):
                content = pattern.sub(lambda m: f"{m.group('indent')}{new_line}", content)
            else:
                # Insert before first $-prefixed prose field, or append
                m = _re.search(r"^[ \t]*\$\w", content, _re.MULTILINE)
                if m:
                    content = content[:m.start()] + new_line + "\n" + content[m.start():]
                else:
                    content = content.rstrip() + "\n" + new_line + "\n"

        entity_path.write_text(content, encoding="utf-8")

        # Incremental reindex for this entity
        with _lock:
            uid = cm.indexer.add_or_update_entity(str(entity_path.parent))
        refreshed = cm.indexer.get(uid) if uid else None
        if refreshed is not None:
            resolved_name = refreshed.name
        elif target_entity is not None:
            resolved_name = target_entity.name
        elif not resolved_name:
            resolved_name = entity_path.parent.name

        log.info(f"Entity field set: {resolved_name}.{field} = {value}")
        return jsonify({"status": "ok", "name": resolved_name, "field": field, "value": value})
    except Exception as e:
        log.exception("write_entity_field")
        return jsonify({"error": str(e)}), 500


@app.route("/entity/clear_fields", methods=["POST"])
def clear_entity_fields():
    """
    Clear the value side of existing entity fields without deleting the fields.

    Request body:
        name / target_npc : str  — entity folder name
        target_npc_id     : str  — preferred when available
        fields            : list[str] — exact field keys to blank (for example "$personality")
        campaign          : str  — optional campaign
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    name = (data.get("name") or data.get("target_npc") or "").strip()
    raw_fields = data.get("fields") or []
    if not isinstance(raw_fields, list) or not raw_fields:
        return jsonify({"error": "'fields' must be a non-empty list"}), 400
    if not name and not any((data.get("target_npc_id"), data.get("persistent_id"), data.get("runtime_id"))):
        return jsonify({"error": "'name' or an ID is required"}), 400

    import pathlib

    try:
        fields = [_safe_field_key(field) for field in raw_fields if str(field or "").strip()]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not fields:
        return jsonify({"error": "'fields' must contain at least one valid field"}), 400

    lookup_data = dict(data)
    if name and not lookup_data.get("target_npc"):
        lookup_data["target_npc"] = name
    target_entity, _ = _resolve_target(lookup_data)

    entity_path = None
    if target_entity is not None:
        entity_path = pathlib.Path(target_entity.path) / "entity.txt"
        if not entity_path.exists():
            entity_path = None

    if entity_path is None and name:
        categories_root = pathlib.Path(cm.active_path) / "categories"
        matches = []
        wanted = name.lower()
        for p in categories_root.rglob("entity.txt"):
            if p.parent.name.lower() == wanted:
                matches.append(p)
        if matches:
            matches.sort(key=lambda p: (
                0 if p.parent.name == name else 1,
                0 if "campaign_npcs" in {part.lower() for part in p.parts} else 1,
                len(p.parts),
            ))
            entity_path = matches[0]

    if entity_path is None:
        return jsonify({"error": f"entity '{name}' not found"}), 404

    resolved_name = name
    try:
        campaign_root = pathlib.Path(cm.active_path)
        _assert_path_within(campaign_root, entity_path, "entity")
        original = entity_path.read_text(encoding="utf-8")
        updated, cleared = _clear_entity_field_values(original, fields)
        changed = updated != original
        if changed:
            entity_path.write_text(updated, encoding="utf-8")
            with _lock:
                uid = cm.indexer.add_or_update_entity(str(entity_path.parent))
            refreshed = cm.indexer.get(uid) if uid else None
            if refreshed is not None:
                resolved_name = refreshed.name
            elif target_entity is not None:
                resolved_name = target_entity.name
            elif not resolved_name:
                resolved_name = entity_path.parent.name
        elif target_entity is not None:
            resolved_name = target_entity.name
        elif not resolved_name:
            resolved_name = entity_path.parent.name

        log.info(f"Entity fields cleared: {resolved_name} -> {cleared}")
        return jsonify({
            "status": "ok",
            "name": resolved_name,
            "cleared_fields": cleared,
            "changed": changed,
        })
    except Exception as e:
        log.exception("clear_entity_fields")
        return jsonify({"error": str(e)}), 500


@app.route("/dialogue/save", methods=["POST"])
def save_dialogue():
    """
    Persist a completed dialogue exchange.
    Call AFTER the LLM has responded.

    Request body:
        target_npc     : str
        target_npc_id  : str  — preferred
        persistent_id  : str  — alias
        runtime_id     : str  — alias
        player_line    : str
        npc_line       : str
    """
    data        = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err
    player_line = (data.get("player_line") or "").strip()
    npc_line    = (data.get("npc_line")    or "").strip()

    entity, _ = _resolve_target(data)
    if entity is None:
        return jsonify({"error": "Entity not found"}), 404

    cm.prompt_builder.save_dialogue_turn(
        entity      = entity,
        player_line = player_line,
        npc_line    = npc_line,
        keep_lines  = cm.config.get("dialogue_keep_lines", 30),
    )
    return jsonify({"status": "ok", "entity": entity.name})


@app.route("/dialogue/replace", methods=["POST"])
def replace_dialogue():
    """
    Replace an entity's dialogue.txt from a complete history list.
    Used by native SS save operations so Kayak remains the source of truth.
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    entity, _ = _resolve_target(data)
    if entity is None:
        return jsonify({"error": "Entity not found"}), 404

    lines = data.get("lines") or []
    if not isinstance(lines, list):
        return jsonify({"error": "'lines' must be a list"}), 400

    keep = _safe_keep_lines(data.get("keep_lines"), cm.config.get("dialogue_keep_lines", 30))
    cm.prompt_builder.replace_dialogue(entity, lines, keep)
    return jsonify({"status": "ok", "entity": entity.name, "lines": len(lines)})


@app.route("/dialogue/cull_future", methods=["POST"])
def cull_future_dialogue():
    """
    Remove dialogue.txt lines after the supplied in-game timestamp.

    This is an operational maintenance action, not retrieval. It scans the
    campaign dialogue files directly from disk, writes the trimmed files, and
    forces a Kayak campaign reload before reporting success.

    Request body:
        day          : int
        hour         : int
        minute       : int
        campaign     : str   optional switch
        dry_run      : bool  if true, report what would be removed only
        reload_after : bool  default true; reload index after writing changes
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    try:
        cutoff_day = int(data.get("day"))
        cutoff_hour = int(data.get("hour"))
        cutoff_minute = int(data.get("minute"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "'day', 'hour', and 'minute' must be integers"}), 400

    if cutoff_day < 0:
        return jsonify({"status": "error", "error": "'day' must be >= 0"}), 400
    if cutoff_hour < 0 or cutoff_hour > 23:
        return jsonify({"status": "error", "error": "'hour' must be between 0 and 23"}), 400
    if cutoff_minute < 0 or cutoff_minute > 59:
        return jsonify({"status": "error", "error": "'minute' must be between 0 and 59"}), 400

    dry_run = bool(data.get("dry_run", False))
    reload_after = bool(data.get("reload_after", True))

    if not cm.active_path:
        return jsonify({"status": "error", "error": "No active campaign path"}), 503

    campaign_root = pathlib.Path(cm.active_path)
    categories_root = campaign_root / "categories"
    scan_roots = [
        categories_root / "campaign_npcs",
        categories_root / "base_npcs",
    ]

    files_scanned = 0
    files_changed = 0
    lines_removed = 0
    errors = []
    changed_files = []

    log.info(
        "dialogue_cull start: campaign=%s cutoff=Day %s %02d:%02d dry_run=%s reload_after=%s",
        cm.active_name,
        cutoff_day,
        cutoff_hour,
        cutoff_minute,
        dry_run,
        reload_after,
    )

    with _lock:
        for root in scan_roots:
            if not root.is_dir():
                continue
            try:
                dialogue_files = sorted(root.rglob("dialogue.txt"))
            except OSError as e:
                errors.append({"path": str(root), "error": str(e)})
                log.warning("dialogue_cull scan failed for %s: %s", root, e)
                continue

            for dialogue_path in dialogue_files:
                try:
                    _assert_path_within(campaign_root, dialogue_path, "dialogue_path")
                except ValueError as e:
                    errors.append({"path": str(dialogue_path), "error": str(e)})
                    continue

                files_scanned += 1
                try:
                    original_text = dialogue_path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    errors.append({"path": str(dialogue_path), "error": str(e)})
                    log.warning("dialogue_cull read failed for %s: %s", dialogue_path, e)
                    continue

                # Preserve line endings as much as possible by splitting with keepends.
                original_lines = original_text.splitlines(keepends=True)
                kept_lines = []
                removed_here = 0
                for line in original_lines:
                    if _dialogue_line_is_future(line, cutoff_day, cutoff_hour, cutoff_minute):
                        removed_here += 1
                    else:
                        kept_lines.append(line)

                if removed_here <= 0:
                    continue

                files_changed += 1
                lines_removed += removed_here
                rel_path = str(dialogue_path.relative_to(campaign_root))
                changed_files.append({"path": rel_path, "lines_removed": removed_here})

                if dry_run:
                    continue

                try:
                    dialogue_path.write_text("".join(kept_lines), encoding="utf-8")
                except OSError as e:
                    errors.append({"path": str(dialogue_path), "error": str(e)})
                    log.warning("dialogue_cull write failed for %s: %s", dialogue_path, e)

        kayak_reloaded = False
        reload_error = None
        if not dry_run and reload_after and lines_removed > 0:
            try:
                cm.reload_index()
                kayak_reloaded = True
                log.info("dialogue_cull reload complete: %s %s", cm.active_name, cm.indexer.stats())
            except Exception as e:
                reload_error = str(e)
                log.exception("dialogue_cull reload failed")

    status = "ok"
    http_status = 200
    if reload_error:
        status = "error"
        http_status = 500

    result = {
        "status": status,
        "campaign": cm.active_name,
        "cutoff": {
            "day": cutoff_day,
            "hour": cutoff_hour,
            "minute": cutoff_minute,
            "label": f"Day {cutoff_day}, {cutoff_hour:02d}:{cutoff_minute:02d}",
        },
        "dry_run": dry_run,
        "reload_after": reload_after,
        "kayak_reloaded": kayak_reloaded,
        "files_scanned": files_scanned,
        "files_changed": files_changed,
        "lines_removed": lines_removed,
        "changed_files": changed_files[:100],
        "changed_files_truncated": len(changed_files) > 100,
        "errors": errors,
        "error_count": len(errors),
    }
    if reload_error:
        result["error"] = (
            "Cull wrote dialogue files but Kayak reload failed. "
            "Restart Kayak/SentientSands before continuing."
        )
        result["reload_error"] = reload_error

    log.info(
        "dialogue_cull complete: status=%s campaign=%s files_scanned=%s files_changed=%s lines_removed=%s errors=%s reloaded=%s",
        status,
        cm.active_name,
        files_scanned,
        files_changed,
        lines_removed,
        len(errors),
        kayak_reloaded,
    )
    return jsonify(result), http_status

@app.route("/dialogue/read", methods=["POST"])
def read_dialogue():
    """
    Return an NPC's conversation history from dialogue.txt.
    Used by ss_persistence.load_existing_profile() as the Kayak read path.

    Request body:
        target_npc     : str
        target_npc_id  : str  — preferred
        persistent_id  : str  — alias
        runtime_id     : str  — alias
        keep_lines     : int  — optional, defaults to dialogue_keep_lines config

    Response:
        { "status": "ok", "lines": ["Player: ...", "NPC: ...", ...] }
        { "error": "..." }  on failure
    """
    data = request.get_json(force=True, silent=True) or {}
    err = _require_active_campaign(data)
    if err:
        return err

    entity, _ = _resolve_target(data)
    if entity is None:
        return jsonify({"error": "Entity not found", "lines": []}), 404

    keep = _safe_keep_lines(data.get("keep_lines"), cm.config.get("dialogue_keep_lines", 30))
    lines = cm.prompt_builder.get_dialogue(entity, keep)
    return jsonify({"status": "ok", "entity": entity.name, "lines": lines})


# ─── EVENT LOG PROCESSING ────────────────────────────────────────────────────

@app.route("/events/parse", methods=["POST"])
def parse_events():
    data     = request.get_json(force=True, silent=True) or {}
    raw_path = (data.get("raw_log_path") or "").strip()
    out_dir  = (data.get("output_dir")   or "").strip()
    if not raw_path or not out_dir:
        return jsonify({"error": "raw_log_path and output_dir are required"}), 400
    try:
        parse_event_log(raw_path, out_dir)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"Kayak root: {KAYAK_ROOT}")
    try:
        cm.ensure_default_campaign()
        log.info(f"Active campaign: {cm.active_name}  {cm.indexer.stats()}")
    except Exception as e:
        log.warning(f"Could not load default campaign: {e}")
        log.warning("Use POST /campaign/create + /campaign/load to get started.")

    host = cm.config.get("host", "127.0.0.1")
    port = int(cm.config.get("port", 5001))
    log.info(f"Kayak listening on {host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
