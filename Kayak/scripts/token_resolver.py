# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak prompt token resolver.

This module is the new explicit prompt-composition layer. It expands only
whitelisted <...> tokens found in user-authored prompt files. It must not call
Retriever.retrieve() and must not use the legacy arbitrary field-expansion graph.

Design law:
  prompt files decide structure;
  SentientSands/Kayak provide metadata;
  token_resolver injects only what was explicitly requested.
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .retriever import extract_keywords


_TOKEN_RE = re.compile(r"<[^<>\n]{1,160}>")
# Необязательный блок промпта: [[текст с <токенами>]]. Если все токены
# внутри пусты, блок выбрасывается целиком — чтобы в промпт не попадали
# строки вида "ты работал как ." или "в регионе , в городе Скуин".
_OPTIONAL_BLOCK_RE = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)
_CHILD_RE = re.compile(r"\s+with\s+(\d+)\s+children\s*$", re.IGNORECASE)
_PLAYER_KEY_RE = re.compile(r"^player_key_entities\s+key\s+(\d+)\s+with\s+(\d+)\s+children$", re.IGNORECASE)
_PLAYER_ENTITY_RE = re.compile(r"^player_entity(?:\s+with\s+(\d+)\s+children)?$", re.IGNORECASE)
_PLAYER_INVENTORY_ITEMS_RE = re.compile(r"^player_inventory_items\s+(\d+)$", re.IGNORECASE)
_TARGET_INVENTORY_ITEMS_RE = re.compile(r"^target_npc_inventory_items\s+(\d+)$", re.IGNORECASE)
_NEARBY_LIST_RE = re.compile(r"^nearby_(names|sapients|squad|non_squad|traders|injured|hostiles)\s+(\d+)$", re.IGNORECASE)
_RECENT_EVENTS_RE = re.compile(r"^recent_(events|local_events|combat_events|trade_events)\s+(\d+)$", re.IGNORECASE)
_SPEAKER_META_RE = re.compile(r"^speaker_(\d+)_(name|id|race|gender|faction|health|gear|personality|category|persona_category)$", re.IGNORECASE)
_SPEAKER_ENTITY_RE = re.compile(r"^speaker_(\d+)_(fac|faction|race)_entity\s+with\s+(\d+)\s+children$", re.IGNORECASE)
_RANDOM_FOLDER_RE = re.compile(r"^random_entity_folder\s+([A-Za-z0-9_\- ]+)\s+with\s+(\d+)\s+children$", re.IGNORECASE)
_DIRECT_ENTITY_RE = re.compile(r"^entity\s+(.+?)\s+with\s+(\d+)\s+children$", re.IGNORECASE)
_WORLD_RUMORS_RE = re.compile(r"^world_rumors\s+(\d+)$", re.IGNORECASE)
_DIALOGUE_LINES_RE = re.compile(r"^dialogue_lines_qty\s+(\d+)$", re.IGNORECASE)
# Штамп реплики в dialogue.txt: "[Day 3, 07:41] Имя: текст".
_DIALOGUE_STAMP_RE = re.compile(r"^\[Day\s+(\d+),\s*(\d{1,2}):(\d{2})\]")
_NEARBY_NPCS_RE = re.compile(r"^nearby_npcs\s+(\d+)$", re.IGNORECASE)
_NPC_NEARBY_RE = re.compile(r"^npc_nearby\s+(\d+)$", re.IGNORECASE)
_CURRENT_LOCATION_FIELD_RE = re.compile(
    r"^current_(town|region)_(name|region|faction|type|description|summary|atmosphere|travel_note|directions|known_for|notable)$",
    re.IGNORECASE,
)

_INVALID_WORLD_NAMES = {
    "", "unknown", "none", "no faction", "unspecified area", "unknown region",
    "unattributed", "nearby", "player", "player's squad",
}

# Технические заглушки профиля. Сервер пишет их, когда данных нет, поэтому
# в промпте они означают отсутствие значения, а не название работы/фракции.
_PLACEHOLDER_METADATA = {
    "none", "unknown", "n/a", "na", "null", "unspecified", "undefined",
}

_STAT_KEYS = (
    "strength", "dexterity", "toughness", "perception",
    "melee_attack", "melee_defence", "dodge", "athletics",
    "stealth", "katanas", "sabres", "hackers", "heavy_weapons",
    "blunt", "polearms", "martial_arts", "crossbows", "turrets",
)

_TARGET_METADATA_ALIASES = {
    "target_npc_name": ("display_name", "name", "target_npc"),
    "target_npc_display_name": ("display_name", "name"),
    "target_npc_original_name": ("original_name", "name"),
    "target_npc_id": ("persistent_id", "id", "runtime_id"),
    "target_npc_race": ("race", "Race"),
    "target_npc_gender": ("gender", "sex", "Sex"),
    "target_npc_faction": ("faction", "Faction", "origin_faction"),
    "target_npc_origin_faction": ("origin_faction", "OriginFaction", "faction"),
    "target_npc_former_faction": ("origin_faction", "OriginFaction"),
    "target_npc_town": ("town", "town_name", "location", "home_town", "city"),
    "target_npc_region": ("region", "home_region"),
    "target_npc_health": ("health", "Health"),
    "target_npc_gear": ("gear", "Gear"),
    "target_npc_relation": ("relation", "Relation"),
    "target_npc_memory": ("memory", "Memory"),
    "target_npc_state": ("character_state", "state", "Status", "status"),
    "target_npc_job": ("job", "Job", "role"),
    "target_npc_money": ("money", "cats"),
    "target_npc_is_trader": ("is_trader",),
    "target_npc_in_shop": ("in_shop",),
    "target_npc_personality": ("personality", "$personality", "Personality"),
    "target_npc_backstory": ("backstory", "$backstory", "Backstory"),
    "target_npc_speech_quirks": ("speech_quirks", "$speech_quirks", "SpeechQuirks"),
}

_RUNTIME_BLOCKS = {
    "current_location",
    "player_status",
    "player_inventory",
    "mode_guidance",
    "final_instruction",
    "language_instruction",
    "chat_output_contract",
    "radiant_output_contract",
    "profile_json_contract",
    "available_action_tags",
}

_PLAYER_CONTEXT_TOKENS = {
    "player_name",
    "player_race",
    "player_gender",
    "player_faction",
    "player_condition",
    "player_health",
    "player_hunger",
    "player_blood",
    "player_stats",
    "player_visible_gear",
    "player_day",
    "player_time",
    "player_money",
    "player_town",
    "player_region",
    "player_is_indoors",
    "player_building",
    "player_location_json",
}

_PLAYER_CONTEXT_ALIASES = {
    "player_name": ("name", "player_name", "display_name"),
    "player_race": ("race", "Race"),
    "player_gender": ("gender", "sex", "Sex"),
    "player_faction": ("faction", "Faction", "factionID"),
    "player_day": ("day", "current_day"),
    "player_money": ("money", "cats", "player_money"),
    "player_town": ("town_name", "town", "location", "city", "area"),
    "player_region": ("biome", "region", "zone"),
    "player_is_indoors": ("indoors", "is_indoors", "in_building"),
    "player_building": ("building_name", "building", "current_building"),
}

_PLAYER_ENTITY_FIELD_ALIASES = {
    "player_personality": ("personality", "$personality", "Personality"),
    "player_backstory": ("backstory", "$backstory", "Backstory"),
    "player_quirks": ("speech_quirks", "$speech_quirks", "SpeechQuirks", "quirks", "$quirks"),
}

_LOCATION_STRUCT_FIELDS = (
    ("region", "Region"),
    ("faction", "Faction"),
    ("type", "Type"),
    ("location", "Location"),
    ("directions", "Directions"),
)

_LOCATION_PROSE_FIELDS = (
    ("description", "Description"),
    ("summary", "Summary"),
    ("atmosphere", "Atmosphere"),
    ("known_for", "Known for"),
    ("travel_note", "Travel note"),
    ("notable", "Notable"),
)


@dataclass
class TokenResolverContext:
    campaign_name: str = ""
    campaign_root: str = ""
    player_message: str = ""
    player_name: str = ""
    target_entity: Any = None
    target_metadata: dict = field(default_factory=dict)
    radiant_speakers: list[dict] = field(default_factory=list)
    nearby_npcs: list[dict] = field(default_factory=list)
    target_shop_stock: list = field(default_factory=list)
    recent_events: list[str] = field(default_factory=list)
    server_town: str = ""
    server_region: str = ""
    world_synthesis_path: str = ""
    world_events_path: str = ""
    # Потолок на число реплик истории. -1 — без потолка; ставится сервером,
    # когда промпт не влезает в бюджет модели.
    dialogue_lines_cap: int = -1
    world_synthesis: str = ""
    player_context: dict = field(default_factory=dict)
    player_status: str = ""
    player_inventory: str = ""
    current_location: str = ""
    mode_guidance: str = ""
    final_instruction: str = ""
    language_instruction: str = ""
    available_action_tags: str = ""
    chat_output_contract: str = ""
    radiant_output_contract: str = ""
    profile_json_contract: str = ""
    campaign_chronicle: str = ""
    # Core prompt section payloads formerly appended automatically by PromptBuilder.
    # These are now injected only through explicit mandatory-prompt tokens.
    world_context: str = ""
    target_npc_context: str = ""
    rng_seed: Optional[int] = None


@dataclass
class ParsedToken:
    raw: str
    kind: str
    args: dict = field(default_factory=dict)


class TokenResolver:
    def __init__(self, indexer, prompt_builder, logger=None):
        self.indexer = indexer
        self.prompt_builder = prompt_builder
        self.logger = logger
        self._prompt_cache: dict[str, str] = {}

    def expand_text(self, text: str, ctx: TokenResolverContext) -> str:
        if not text:
            return text or ""
        # Prompt-local cache. expand_text() is one prompt text block; callers may reuse
        # a resolver for more blocks, so do not use process-global caching.
        self._prompt_cache = {}
        out = str(text)
        if "[[" in out and "]]" in out:
            out = self.resolve_optional_blocks(out, ctx)
        if "<" not in out or ">" not in out:
            return out
        for raw in sorted(set(m.group(0) for m in _TOKEN_RE.finditer(out)), key=len, reverse=True):
            token = self.parse_token(raw)
            if not token:
                continue  # unknown HTML/XML-ish prompt markup stays untouched
            repl = self.resolve_token(token, ctx)
            out = out.replace(raw, repl)
        return out

    def find_tokens(self, text: str) -> list[ParsedToken]:
        return [t for t in (self.parse_token(m.group(0)) for m in _TOKEN_RE.finditer(text or "")) if t]

    def parse_token(self, raw_token: str) -> Optional[ParsedToken]:
        raw = str(raw_token or "").strip()
        if not (raw.startswith("<") and raw.endswith(">")):
            return None
        body = self.normalize_token_whitespace(raw[1:-1])
        lower = body.lower()

        m = _PLAYER_KEY_RE.match(body)
        if m:
            return ParsedToken(raw, "player_key_entities", {"key_index": int(m.group(1)), "child_count": int(m.group(2))})

        m = _PLAYER_ENTITY_RE.match(body)
        if m:
            return ParsedToken(raw, "player_entity", {"child_count": int(m.group(1) or 0)})

        m = _PLAYER_INVENTORY_ITEMS_RE.match(body)
        if m:
            return ParsedToken(raw, "player_inventory_items", {"count": int(m.group(1))})

        m = _TARGET_INVENTORY_ITEMS_RE.match(body)
        if m:
            return ParsedToken(raw, "target_npc_inventory_items", {"count": int(m.group(1))})

        m = _NEARBY_LIST_RE.match(body)
        if m:
            return ParsedToken(raw, "nearby_list", {"list_type": m.group(1).lower(), "count": int(m.group(2))})

        m = _RECENT_EVENTS_RE.match(body)
        if m:
            return ParsedToken(raw, "recent_events", {"event_type": m.group(1).lower(), "count": int(m.group(2))})

        m = _SPEAKER_META_RE.match(body)
        if m:
            return ParsedToken(raw, "speaker_metadata", {"speaker_index": int(m.group(1)), "field": m.group(2).lower()})

        m = _SPEAKER_ENTITY_RE.match(body)
        if m:
            kind = "speaker_race_entity" if m.group(2).lower() == "race" else "speaker_fac_entity"
            return ParsedToken(raw, kind, {"speaker_index": int(m.group(1)), "child_count": int(m.group(3))})

        m = _WORLD_RUMORS_RE.match(body)
        if m:
            return ParsedToken(raw, "world_rumors", {"count": int(m.group(1))})

        m = _DIALOGUE_LINES_RE.match(body)
        if m:
            return ParsedToken(raw, "dialogue_lines_qty", {"count": int(m.group(1))})

        m = _NEARBY_NPCS_RE.match(body)
        if m:
            return ParsedToken(raw, "nearby_npcs", {"count": int(m.group(1))})

        m = _NPC_NEARBY_RE.match(body)
        if m:
            return ParsedToken(raw, "npc_nearby", {"index": int(m.group(1))})

        m = _CURRENT_LOCATION_FIELD_RE.match(body)
        if m:
            return ParsedToken(raw, "current_location_field", {"scope": m.group(1).lower(), "field": m.group(2).lower()})

        if lower in ("nearby_count", "closest_npc"):
            return ParsedToken(raw, lower, {})

        if lower in (
            "target_npc_shop_stock",
            "target_npc_visible_gear",
            "target_npc_stats",
            "target_npc_injuries",
            "target_npc_traits",
        ):
            return ParsedToken(raw, lower, {})

        if lower in ("campaign_chronicle", "world_synthesis"):
            return ParsedToken(raw, "campaign_block", {"field": lower})

        if lower in ("world_context", "target_npc_context"):
            return ParsedToken(raw, "core_block", {"field": lower})

        if lower == "latest_rumor":
            return ParsedToken(raw, "world_rumors", {"count": 1})
        if lower in ("violence_summary", "trade_summary"):
            return ParsedToken(raw, lower, {})
        if lower in _TARGET_METADATA_ALIASES:
            return ParsedToken(raw, "target_metadata", {"field": lower})
        if lower in _PLAYER_ENTITY_FIELD_ALIASES:
            return ParsedToken(raw, "player_entity_field", {"field": lower})
        if lower == "target_npc_profile":
            return ParsedToken(raw, "target_npc_profile", {})
        if lower in _RUNTIME_BLOCKS:
            return ParsedToken(raw, "runtime_block", {"field": lower})
        if lower in _PLAYER_CONTEXT_TOKENS:
            return ParsedToken(raw, "player_context", {"field": lower})

        for prefix, kind in (
            ("player_fac_entity", "player_fac_entity"),
            ("player_faction_entity", "player_fac_entity"),
            ("target_npc_fac_entity", "target_npc_fac_entity"),
            ("target_npc_faction_entity", "target_npc_fac_entity"),
            ("target_npc_race_entity", "target_npc_race_entity"),
            ("target_npc_town_entity", "target_npc_town_entity"),
            ("target_npc_region_entity", "target_npc_region_entity"),
            ("town_entity", "town_entity"),
            ("region_entity", "region_entity"),
            ("active_faction_entity", "active_faction_entity"),
            ("random_entity_all", "random_entity_all"),
        ):
            if lower.startswith(prefix):
                m = _CHILD_RE.search(body)
                if m and lower[:m.start()].strip() == prefix:
                    return ParsedToken(raw, kind, {"child_count": int(m.group(1))})

        m = _RANDOM_FOLDER_RE.match(body)
        if m:
            return ParsedToken(raw, "random_entity_folder", {"folder_name": m.group(1).strip(), "child_count": int(m.group(2))})

        m = _DIRECT_ENTITY_RE.match(body)
        if m:
            return ParsedToken(raw, "direct_entity", {"name": m.group(1).strip(), "child_count": int(m.group(2))})

        return None

    def resolve_token(self, token: ParsedToken, ctx: TokenResolverContext) -> str:
        key = self.make_cache_key(token, ctx)
        if key in self._prompt_cache:
            return self._prompt_cache[key]
        try:
            kind = token.kind
            if kind == "target_metadata":
                result = self.resolve_target_metadata(ctx, token.args["field"])
            elif kind == "target_npc_profile":
                result = self.render_single_entity(ctx.target_entity) if ctx.target_entity is not None else ""
            elif kind == "runtime_block":
                result = self.resolve_runtime_block(ctx, token.args["field"])
            elif kind == "current_location_field":
                result = self.resolve_current_location_field(ctx, token.args["scope"], token.args["field"])
            elif kind == "player_context":
                result = self.resolve_player_context_token(ctx, token.args["field"])
            elif kind == "core_block":
                result = self.resolve_core_block(ctx, token.args["field"])
            elif kind == "dialogue_lines_qty":
                result = self.resolve_dialogue_lines(ctx, token.args["count"])
            elif kind == "nearby_npcs":
                result = self.resolve_nearby_npcs(ctx, token.args["count"])
            elif kind == "npc_nearby":
                result = self.resolve_npc_nearby(ctx, token.args["index"])
            elif kind == "speaker_metadata":
                result = self.resolve_speaker_metadata(ctx, token.args["speaker_index"], token.args["field"])
            elif kind == "player_key_entities":
                result = self.resolve_player_key_entities(ctx, token.args["key_index"], token.args["child_count"])
            elif kind == "player_entity":
                result = self.resolve_player_entity(ctx, token.args["child_count"])
            elif kind == "player_entity_field":
                result = self.resolve_player_entity_field(ctx, token.args["field"])
            elif kind == "player_fac_entity":
                result = self.resolve_player_faction_entity(ctx, token.args["child_count"])
            elif kind == "player_inventory_items":
                result = self.resolve_player_inventory_items(ctx, token.args["count"])
            elif kind == "target_npc_inventory_items":
                result = self.resolve_target_inventory_items(ctx, token.args["count"])
            elif kind == "nearby_list":
                result = self.resolve_nearby_list(ctx, token.args["list_type"], token.args["count"])
            elif kind == "nearby_count":
                result = str(len(self.get_nearby_npcs(ctx)))
            elif kind == "closest_npc":
                result = self.resolve_closest_npc(ctx)
            elif kind == "recent_events":
                result = self.resolve_recent_events(ctx, token.args["event_type"], token.args["count"])
            elif kind == "target_npc_shop_stock":
                result = self.resolve_target_shop_stock(ctx)
            elif kind == "target_npc_visible_gear":
                result = self.resolve_target_visible_gear(ctx)
            elif kind == "target_npc_stats":
                result = self.resolve_target_stats(ctx)
            elif kind == "target_npc_injuries":
                result = self.resolve_target_injuries(ctx)
            elif kind == "target_npc_traits":
                result = self.resolve_target_traits(ctx)
            elif kind == "campaign_block":
                result = self.resolve_campaign_block(ctx, token.args["field"])
            elif kind == "target_npc_fac_entity":
                result = self.resolve_entity_from_target_field(ctx, ("faction", "Faction", "origin_faction"), token.args["child_count"])
            elif kind == "target_npc_race_entity":
                result = self.resolve_entity_from_target_field(ctx, ("race", "Race"), token.args["child_count"])
            elif kind == "target_npc_town_entity":
                result = self.resolve_entity_from_target_field(ctx, ("town", "town_name", "location", "home_town", "city"), token.args["child_count"])
            elif kind == "target_npc_region_entity":
                result = self.resolve_named_entity_text(self.resolve_target_region(ctx), token.args["child_count"])
            elif kind == "speaker_fac_entity":
                result = self.resolve_speaker_entity(ctx, token.args["speaker_index"], ("faction",), token.args["child_count"])
            elif kind == "speaker_race_entity":
                result = self.resolve_speaker_entity(ctx, token.args["speaker_index"], ("race",), token.args["child_count"])
            elif kind == "town_entity":
                result = self.resolve_named_entity_text(self.resolve_town_from_context(ctx), token.args["child_count"])
            elif kind == "region_entity":
                result = self.resolve_named_entity_text(self.resolve_region_from_context(ctx), token.args["child_count"])
            elif kind == "active_faction_entity":
                exclude = {self.resolve_target_metadata(ctx, "target_npc_faction").lower()}
                result = self.resolve_named_entity_text(self.extract_latest_active_faction(self.get_world_synthesis_tail(ctx), exclude), token.args["child_count"])
            elif kind == "world_rumors":
                # Слухи живут в world_events.txt; лог синтеза оставлен запасным
                # вариантом для старых кампаний.
                _rumors = self.extract_rumor_entries(
                    self.get_world_events_tail(ctx), token.args["count"])
                if not _rumors:
                    _rumors = self.extract_recent_rumors(
                        self.get_world_synthesis_tail(ctx), token.args["count"])
                result = self.render_rumors(_rumors)
            elif kind == "violence_summary":
                result = self.extract_latest_violence_summary(self.get_world_synthesis_tail(ctx)) or ""
            elif kind == "trade_summary":
                result = self.extract_latest_trade_summary(self.get_world_synthesis_tail(ctx)) or ""
            elif kind == "random_entity_all":
                result = self.resolve_random_entity_all(ctx, token.args["child_count"])
            elif kind == "random_entity_folder":
                result = self.resolve_random_entity_folder(ctx, token.args["folder_name"], token.args["child_count"])
            elif kind == "direct_entity":
                result = self.resolve_named_entity_text(token.args["name"], token.args["child_count"])
            else:
                result = ""
        except Exception as exc:
            result = ""
            self.log_token_miss(token, f"exception: {exc}")
        result = self.clean_replacement_text(result)
        self._prompt_cache[key] = result
        if result:
            self.log_token_hit(token, result[:80])
        else:
            self.log_token_miss(token, "empty")
        return result

    def resolve_optional_blocks(self, text: str, ctx: TokenResolverContext) -> str:
        """Раскрыть [[...]]: блок остаётся, если внутри есть хоть один
        непустой токен; иначе выбрасывается вместе с лишними переносами."""
        def _replace(match):
            inner = match.group(1)
            tokens = [
                parsed for parsed in (
                    self.parse_token(m.group(0)) for m in _TOKEN_RE.finditer(inner)
                ) if parsed
            ]
            if not tokens:
                return inner
            for token in tokens:
                value = str(self.resolve_token(token, ctx) or "").strip()
                if value and value.lower() not in _PLACEHOLDER_METADATA:
                    return inner
            return ""

        out = _OPTIONAL_BLOCK_RE.sub(_replace, text)
        return re.sub(r"\n{3,}", "\n\n", out)

    def resolve_target_metadata(self, ctx: TokenResolverContext, field_name: str) -> str:
        if field_name == "target_npc_region":
            return self.resolve_target_region(ctx)
        if field_name == "target_npc_former_faction":
            return self.resolve_target_former_faction(ctx)
        meta = self.target_meta(ctx)
        for key in _TARGET_METADATA_ALIASES.get(field_name, (field_name,)):
            val = meta.get(key)
            if val in (None, ""):
                continue
            text = str(val).strip()
            if text.lower() in _PLACEHOLDER_METADATA:
                continue
            return text
        return ""

    def resolve_target_former_faction(self, ctx: TokenResolverContext) -> str:
        """Бывшая фракция — только если она известна и отличается от нынешней."""
        meta = self.target_meta(ctx)

        def _pick(*keys):
            for key in keys:
                val = meta.get(key)
                if val in (None, ""):
                    continue
                text = str(val).strip()
                if text.lower() in _PLACEHOLDER_METADATA:
                    continue
                return text
            return ""

        former = _pick("origin_faction", "OriginFaction")
        if not former:
            return ""
        current = _pick("faction", "Faction")
        if current and former.casefold() == current.casefold():
            return ""
        return former

    def resolve_target_region(self, ctx: TokenResolverContext) -> str:
        meta = self.target_meta(ctx)
        for key in _TARGET_METADATA_ALIASES.get("target_npc_region", ("region", "home_region")):
            val = meta.get(key)
            if val not in (None, ""):
                return str(val).strip()

        town = ""
        for key in _TARGET_METADATA_ALIASES.get("target_npc_town", ()):
            val = meta.get(key)
            if val not in (None, ""):
                town = str(val).strip()
                break
        if town:
            town_entity = self.resolve_entity_by_name(town)
            region = self.entity_field_value(town_entity, "region")
            if region:
                return region
        return ""

    def resolve_speaker_metadata(self, ctx: TokenResolverContext, speaker_index: int, field_name: str) -> str:
        speakers = ctx.radiant_speakers or []
        if speaker_index < 1 or speaker_index > len(speakers):
            return ""
        sp = speakers[speaker_index - 1] or {}
        if field_name == "category":
            field_name = "persona_category"
        return str(sp.get(field_name) or "").strip()

    def resolve_runtime_block(self, ctx: TokenResolverContext, block_name: str) -> str:
        if block_name == "current_location":
            return self.resolve_current_location(ctx) or str(getattr(ctx, block_name, "") or "").strip()
        return str(getattr(ctx, block_name, "") or "").strip()

    def resolve_current_location(self, ctx: TokenResolverContext) -> str:
        bundle = self.current_location_entity_bundle(ctx)
        loc = bundle["loc"]
        if not loc.get("known") and not loc.get("indoors"):
            return ""

        live_lines = []
        area = str(loc.get("tag") or "Unknown").strip()
        live_lines.append(f"- Area: {area}")
        if loc.get("town"):
            live_lines.append(f"- Settlement: {loc.get('town')}")
        if loc.get("region"):
            live_lines.append(f"- Region: {loc.get('region')}")
        if loc.get("building"):
            live_lines.append(f"- Building: {loc.get('building')}")

        context_tags = []
        if loc.get("indoors"):
            context_tags.append("Indoors")
        if loc.get("in_town") and loc.get("town"):
            context_tags.append("Inside a settlement")
        if context_tags:
            live_lines.append(f"- Context: {', '.join(context_tags)}")

        sections = ["\n".join(live_lines)]
        town_name = bundle["town_name"]
        region_name = bundle["region_name"]
        town_entity = bundle["town_entity"]
        region_entity = bundle["region_entity"]

        town_excerpt = self.render_location_entity_excerpt("LOCATION ENTITY", town_entity, town_name)
        if town_excerpt:
            sections.append(town_excerpt)

        if region_entity is not None:
            town_uid = str(getattr(town_entity, "uid", "") or "")
            region_uid = str(getattr(region_entity, "uid", "") or "")
            if not town_uid or region_uid != town_uid:
                region_excerpt = self.render_location_entity_excerpt("REGION ENTITY", region_entity, region_name)
                if region_excerpt:
                    sections.append(region_excerpt)

        return "--- CURRENT_LOCATION\n" + "\n\n".join(section for section in sections if section.strip())

    def resolve_current_location_field(self, ctx: TokenResolverContext, scope: str, field: str) -> str:
        bundle = self.current_location_entity_bundle(ctx)
        scope = str(scope or "").lower()
        field = str(field or "").lower()
        loc = bundle["loc"]
        entity = bundle["town_entity"] if scope == "town" else bundle["region_entity"]

        if field == "name":
            fallback = loc.get("town") if scope == "town" else loc.get("region")
            return str(getattr(entity, "display_name", "") or fallback or "").strip()

        if field == "region" and scope == "town":
            return self.entity_field_value(entity, "region") or str(bundle.get("region_name") or "").strip()

        return self.entity_field_value(entity, field)

    def current_location_entity_bundle(self, ctx: TokenResolverContext) -> dict:
        cache_key = "__current_location_entity_bundle__"
        cached = self._prompt_cache.get(cache_key)
        if isinstance(cached, dict):
            return cached

        loc = self.extract_player_location_context(ctx)
        town_name = str(loc.get("town") or "").strip()
        region_name = str(loc.get("region") or "").strip()
        town_entity = self.resolve_entity_by_name(town_name) if town_name else None

        if not region_name and town_entity is not None:
            region_name = self.entity_field_value(town_entity, "region")
            if region_name:
                loc = dict(loc)
                loc["region"] = region_name
                loc["known"] = True
                loc["tag"] = f"{town_name} (within {region_name})" if town_name else region_name

        region_entity = self.resolve_entity_by_name(region_name) if region_name else None
        bundle = {
            "loc": loc,
            "town_name": town_name,
            "region_name": region_name,
            "town_entity": town_entity,
            "region_entity": region_entity,
        }
        self._prompt_cache[cache_key] = bundle
        return bundle

    def current_location_entity_uids(self, ctx: TokenResolverContext) -> set[str]:
        bundle = self.current_location_entity_bundle(ctx)
        out = set()
        for key in ("town_entity", "region_entity"):
            ent = bundle.get(key)
            uid = str(getattr(ent, "uid", "") or "").strip()
            if uid:
                out.add(uid)
        return out

    def target_profile_entity_uids(self, ctx: TokenResolverContext) -> set[str]:
        """Сущности, которые промпт уже показывает отдельными токенами.

        Раса, фракция, бывшая фракция и город цели попадают в промпт через
        <target_npc_race_entity>, <target_npc_faction_entity> и соседние
        токены. Белый список знаний NPC состоит в основном из них же, поэтому
        без этого исключения те же описания приезжают вторым экземпляром
        внутри <world_context>.
        """
        meta = self.target_meta(ctx) or {}
        out: set[str] = set()
        for field in (
            "race", "Race",
            "faction", "Faction",
            "origin_faction", "OriginFaction",
            "town", "town_name", "location", "home_town", "city",
        ):
            value = str(meta.get(field) or "").strip()
            if not value:
                continue
            entity = self.resolve_entity_by_name(value)
            uid = str(getattr(entity, "uid", "") or "").strip()
            if uid:
                out.add(uid)
        return out

    def filter_current_location_keywords(self, ctx: TokenResolverContext, keywords: list[str]) -> list[str]:
        excluded_uids = self.current_location_entity_uids(ctx)
        filtered = []
        seen = set()
        for keyword in keywords or []:
            clean = str(keyword or "").strip()
            if not clean:
                continue
            if self.keyword_matches_current_location(ctx, clean, excluded_uids):
                continue
            ent = self.resolve_entity_by_name(clean)
            uid = str(getattr(ent, "uid", "") or "").strip() if ent is not None else ""
            if uid and uid in excluded_uids:
                continue
            norm = clean.lower()
            if norm in seen:
                continue
            seen.add(norm)
            filtered.append(clean)
        return filtered

    def keyword_matches_current_location(self, ctx: TokenResolverContext, keyword: str, excluded_uids: set[str] | None = None) -> bool:
        clean = self.normalize_loose_name(keyword)
        if not clean:
            return False
        bundle = self.current_location_entity_bundle(ctx)
        names = [
            bundle.get("town_name"),
            bundle.get("region_name"),
        ]
        for ent_key in ("town_entity", "region_entity"):
            ent = bundle.get(ent_key)
            if ent is None:
                continue
            names.extend([
                getattr(ent, "display_name", ""),
                getattr(ent, "name", ""),
                self.entity_field_value(ent, "display_name"),
            ])

        for name in names:
            norm = self.normalize_loose_name(name)
            if not norm:
                continue
            variants = {norm}
            if norm.startswith("the "):
                variants.add(norm[4:])
            if clean in variants:
                return True

        ent = self.resolve_entity_by_name(keyword)
        uid = str(getattr(ent, "uid", "") or "").strip() if ent is not None else ""
        return bool(uid and uid in (excluded_uids or self.current_location_entity_uids(ctx)))

    def normalize_loose_name(self, value: str) -> str:
        text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
        return re.sub(r"\s+", " ", text)

    def extract_player_location_context(self, ctx: TokenResolverContext) -> dict:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        env = pc.get("environment") if isinstance(pc.get("environment"), dict) else {}

        town = str(self.get_player_context_value(ctx, "player_town") or "").strip()
        region = str(self.get_player_context_value(ctx, "player_region") or "").strip()
        building = str(self.get_player_context_value(ctx, "player_building") or "").strip()
        indoors = (
            self.context_bool(env, "indoors")
            or self.context_bool(env, "is_indoors")
            or self.context_bool(env, "in_building")
            or self.context_bool(pc, "indoors")
            or self.context_bool(pc, "is_indoors")
            or self.context_bool(pc, "in_building")
        )
        in_town = (
            self.context_bool(env, "in_town")
            or self.context_bool(env, "is_in_town")
            or self.context_bool(pc, "in_town")
            or self.context_bool(pc, "is_in_town")
            or bool(town)
        )

        if town and region:
            tag = f"{town} (within {region})"
        elif town:
            tag = town
        elif region:
            tag = region
        else:
            tag = ""

        return {
            "town": town,
            "region": region,
            "building": building,
            "indoors": indoors,
            "in_town": in_town,
            "known": bool(tag),
            "tag": tag,
        }

    def resolve_player_context_token(self, ctx: TokenResolverContext, field_name: str) -> str:
        """Resolve granular player-context tokens from the raw PLAYER_CONTEXT snapshot.

        These tokens expose only known-safe fields. They do not promise weather
        or climate data; location data comes from the existing environment /
        location context if the hook provides it.
        """
        if field_name == "player_time":
            return self.format_player_time(ctx)
        if field_name == "player_location_json":
            return self.render_player_location_json(ctx)
        if field_name in ("player_condition", "player_health"):
            return self.resolve_player_condition(ctx)
        if field_name == "player_hunger":
            return self.resolve_player_hunger(ctx)
        if field_name == "player_blood":
            return self.resolve_player_blood(ctx)
        if field_name == "player_stats":
            return self.resolve_player_stats(ctx)
        if field_name == "player_visible_gear":
            return self.resolve_player_visible_gear(ctx)
        value = self.get_player_context_value(ctx, field_name)
        return self.format_context_value(value)

    def resolve_player_condition(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        return self.format_medical_condition(pc.get("medical") if isinstance(pc.get("medical"), dict) else {}, pc)

    def resolve_player_hunger(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        med = pc.get("medical") if isinstance(pc.get("medical"), dict) else {}
        hunger = med.get("hunger", pc.get("hunger", ""))
        if hunger in (None, ""):
            return ""
        try:
            val = float(hunger)
            if val < 80:
                label = "starving"
            elif val < 200:
                label = "very hungry"
            elif val < 250:
                label = "hungry"
            else:
                label = "fed"
            return f"{int(val)} ({label})"
        except Exception:
            return str(hunger).strip()

    def resolve_player_blood(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        med = pc.get("medical") if isinstance(pc.get("medical"), dict) else {}
        return self.format_blood_value(med)

    def resolve_player_stats(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        return self.render_stats_block(pc)

    def resolve_player_visible_gear(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        return self.render_inventory_items(pc.get("inventory", []), None, equipped=True)

    def resolve_player_inventory_items(self, ctx: TokenResolverContext, count: int) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        return self.render_inventory_items(pc.get("inventory", []), count, equipped=False)

    def get_player_context_value(self, ctx: TokenResolverContext, field_name: str):
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        env = pc.get("environment") if isinstance(pc.get("environment"), dict) else {}
        if field_name == "player_name" and ctx.player_name:
            return ctx.player_name
        for key in _PLAYER_CONTEXT_ALIASES.get(field_name, (field_name,)):
            if key in pc and pc.get(key) not in (None, ""):
                return pc.get(key)
            if key in env and env.get(key) not in (None, ""):
                return env.get(key)
        if field_name == "player_town" and ctx.server_town:
            return ctx.server_town
        if field_name == "player_region" and ctx.server_region:
            return ctx.server_region
        return ""

    def format_player_time(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        day = pc.get("day", "")
        hour = pc.get("hour", "")
        minute = pc.get("minute", "")
        if day in (None, "") and hour in (None, "") and minute in (None, ""):
            return ""
        try:
            hh = f"{int(hour):02d}" if hour not in (None, "") else "??"
        except Exception:
            hh = str(hour)
        try:
            mm = f"{int(minute):02d}" if minute not in (None, "") else "??"
        except Exception:
            mm = str(minute)
        if day not in (None, ""):
            return f"Day {day}, {hh}:{mm}"
        return f"{hh}:{mm}"

    def render_player_location_json(self, ctx: TokenResolverContext) -> str:
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        env = pc.get("environment") if isinstance(pc.get("environment"), dict) else {}
        resolved = {
            "town": self.format_context_value(self.get_player_context_value(ctx, "player_town")),
            "region": self.format_context_value(self.get_player_context_value(ctx, "player_region")),
            "building": self.format_context_value(self.get_player_context_value(ctx, "player_building")),
            "is_indoors": self.format_context_value(self.get_player_context_value(ctx, "player_is_indoors")),
        }
        payload = {"resolved": resolved, "environment": env}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def format_context_value(self, value) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value).strip()

    def format_blood_value(self, medical: dict) -> str:
        if not isinstance(medical, dict):
            return ""
        blood = medical.get("blood", "")
        max_blood = medical.get("max_blood", "")
        if blood in (None, ""):
            return ""
        try:
            blood_f = float(blood)
            max_f = float(max_blood or 100)
            pct = int((blood_f / max_f) * 100) if max_f > 0 else int(blood_f)
            return f"{pct}% ({int(blood_f)}/{int(max_f)})" if max_f else f"{pct}%"
        except Exception:
            return str(blood).strip()

    def format_medical_condition(self, medical: dict, context: dict | None = None) -> str:
        context = context if isinstance(context, dict) else {}
        medical = medical if isinstance(medical, dict) else {}
        state = str(context.get("character_state") or context.get("state") or "").strip().lower()
        if state and state != "normal":
            return state
        status = []
        hunger = medical.get("hunger", context.get("hunger", ""))
        try:
            h = float(hunger)
            if h < 80:
                status.append("starving")
            elif h < 200:
                status.append("very hungry")
            elif h < 250:
                status.append("hungry")
        except Exception:
            pass
        blood = medical.get("blood", context.get("blood", ""))
        max_blood = medical.get("max_blood", context.get("max_blood", 100))
        blood_rate = medical.get("blood_rate", context.get("blood_rate", 0))
        try:
            if float(blood_rate or 0) > 0.01:
                status.append("bleeding")
            elif blood not in (None, ""):
                max_f = float(max_blood or 100)
                pct = float(blood) / max_f if max_f > 0 else 1.0
                if pct < 0.5:
                    status.append("critical bloodloss")
                elif pct < 0.85:
                    status.append("injured")
        except Exception:
            pass
        if medical.get("is_unconscious") or context.get("is_unconscious"):
            status.append("unconscious")
        if context.get("health") and str(context.get("health")).strip().lower() not in ("healthy", "ok"):
            status.append(str(context.get("health")).strip())
        return ", ".join(dict.fromkeys(status)) if status else "healthy"

    def render_inventory_items(self, inventory, count: int | None, equipped: bool | None = None) -> str:
        if not isinstance(inventory, list):
            return ""
        rows = []
        for item in inventory:
            if not isinstance(item, dict):
                continue
            if equipped is not None and bool(item.get("equipped")) != equipped:
                continue
            name = str(item.get("name") or item.get("Name") or "Unknown Item").strip()
            if not name:
                continue
            try:
                item_count = int(item.get("count", 1))
            except Exception:
                item_count = 1
            bits = [f"{name} (x{item_count})"]
            slot = str(item.get("slot") or "").strip()
            if equipped and slot:
                bits.append(f"[{slot.upper()}]")
            price = item.get("price")
            if price not in (None, ""):
                bits.append(f"[value: {price} cats]")
            rows.append("- " + " ".join(bits))
            if count is not None and len(rows) >= max(0, count):
                break
        return "\n".join(rows)

    def target_meta(self, ctx: TokenResolverContext) -> dict:
        meta = dict(ctx.target_metadata or {})
        ent = ctx.target_entity
        if ent is not None:
            meta.setdefault("name", getattr(ent, "name", ""))
            meta.setdefault("display_name", getattr(ent, "display_name", ""))
            meta.setdefault("id", getattr(ent, "best_id", ""))
            for k, v in getattr(ent, "fields", {}).items():
                meta.setdefault(k.lstrip("$"), v)
                meta.setdefault(k, v)
        return meta

    def resolve_core_block(self, ctx: TokenResolverContext, block_name: str) -> str:
        """Return core section payloads without hardcoded section labels.

        The mandatory prompt decides labels/headings and placement.
        """
        return str(getattr(ctx, block_name, "") or "").strip()

    def drop_abandoned_timeline(self, lines: list, ctx: TokenResolverContext) -> list:
        """Убрать реплики, помеченные временем позже текущего игрового.

        Kenshi позволяет загрузить ранний сейв, и день откатывается назад.
        dialogue.txt при этом общий: в промпт попадают разговоры из брошенной
        ветки времени. NPC «помнит» то, чего в этой линии не было, и склонен
        повторить оттуда готовый ответ вместо ответа на заданный вопрос.

        Без внятного игрового времени ничего не отсеиваем.
        """
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        try:
            now = (int(pc.get("day")), int(pc.get("hour")), int(pc.get("minute")))
        except (TypeError, ValueError):
            return lines
        if now[0] <= 0:
            return lines
        kept = []
        for line in lines:
            m = _DIALOGUE_STAMP_RE.match(line)
            if m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) > now:
                continue
            kept.append(line)
        return kept

    def resolve_dialogue_lines(self, ctx: TokenResolverContext, count: int) -> str:
        """Inject the target NPC dialogue history, bounded by the token value.

        Example: <dialogue_lines_qty 15> injects the last 15 cleaned dialogue lines.
        """
        cap = getattr(ctx, "dialogue_lines_cap", -1)
        if isinstance(cap, int) and cap >= 0:
            count = min(count, cap)
        if count <= 0 or ctx.target_entity is None or not self.prompt_builder:
            return ""
        try:
            # С запасом: часть строк может отсеяться как след другого сейва.
            raw = self.prompt_builder.load_dialogue(ctx.target_entity, keep_lines=count * 3)
        except Exception:
            return ""
        lines = [line for line in raw.splitlines() if line.strip()]
        lines = self.drop_abandoned_timeline(lines, ctx)
        return "\n".join(lines[-count:])


    def get_nearby_npcs(self, ctx: TokenResolverContext) -> list[dict]:
        """Return nearby NPC metadata from the cleanest available source.

        Preferred source is explicit ctx.nearby_npcs supplied by SentientSands.
        Fallbacks use target metadata / player context because older payloads may
        carry nearby data there. This is metadata only; no entity lookup happens.
        """
        candidates = (
            ctx.nearby_npcs,
            (ctx.target_metadata or {}).get("nearby"),
            (ctx.player_context or {}).get("nearby") if isinstance(ctx.player_context, dict) else None,
        )
        for value in candidates:
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    def resolve_nearby_npcs(self, ctx: TokenResolverContext, count: int) -> str:
        if count <= 0:
            return ""
        nearby = self.get_nearby_npcs(ctx)[:count]
        return "\n".join(self.render_nearby_npc(npc) for npc in nearby if npc)

    def resolve_npc_nearby(self, ctx: TokenResolverContext, index: int) -> str:
        if index <= 0:
            return ""
        nearby = self.get_nearby_npcs(ctx)
        if index > len(nearby):
            return ""
        return self.render_nearby_npc(nearby[index - 1])

    def render_nearby_npc(self, npc: dict) -> str:
        name = str(npc.get("name") or npc.get("display_name") or "Someone").strip()
        race = str(npc.get("race") or npc.get("Race") or "Unknown").strip()
        gender = str(npc.get("gender") or npc.get("sex") or npc.get("Sex") or "Unknown").strip()
        faction = str(npc.get("faction") or npc.get("Faction") or "Unknown").strip()
        health = str(npc.get("health") or npc.get("Health") or "").strip()
        gear = str(npc.get("equipment") or npc.get("gear") or npc.get("Gear") or "").strip()
        dist_val = npc.get("dist", npc.get("distance", ""))
        dist = ""
        try:
            if dist_val not in (None, ""):
                fdist = float(dist_val)
                dist = "Immediate proximity" if fdist < 2.5 else f"{int(fdist)}m away"
        except Exception:
            dist = str(dist_val).strip()

        parts = []
        identity = ", ".join(x for x in (gender, race, faction) if x and x.lower() != "unknown")
        if identity:
            parts.append(f"{name} ({identity})")
        else:
            parts.append(name)
        if health:
            parts.append(f"Health: {health}")
        if dist:
            parts.append(dist)
        if gear:
            parts.append(f"Visible Gear: {gear}")
        return "- " + " | ".join(parts)

    def resolve_closest_npc(self, ctx: TokenResolverContext) -> str:
        nearby = self.get_nearby_npcs(ctx)
        if not nearby:
            return ""
        def sort_key(npc):
            try:
                return float(npc.get("dist", npc.get("distance", 999999)) or 999999)
            except Exception:
                return 999999.0
        return self.render_nearby_npc(sorted(nearby, key=sort_key)[0])

    def resolve_nearby_list(self, ctx: TokenResolverContext, list_type: str, count: int) -> str:
        nearby = self.get_nearby_npcs(ctx)
        player_faction = str(self.get_player_context_value(ctx, "player_faction") or "").strip().lower()
        filtered = []
        for npc in nearby:
            if list_type == "names":
                filtered.append(npc)
            elif list_type == "sapients" and self.nearby_is_sapient(npc):
                filtered.append(npc)
            elif list_type == "squad" and self.nearby_is_player_squad(npc, player_faction):
                filtered.append(npc)
            elif list_type == "non_squad" and not self.nearby_is_player_squad(npc, player_faction):
                filtered.append(npc)
            elif list_type == "traders" and self.context_bool(npc, "is_trader", "trader"):
                filtered.append(npc)
            elif list_type == "injured" and self.nearby_is_injured(npc):
                filtered.append(npc)
            elif list_type == "hostiles" and self.nearby_is_hostile(npc):
                filtered.append(npc)
        return "\n".join(self.render_nearby_name_line(npc) for npc in filtered[:max(0, count)])

    def render_nearby_name_line(self, npc: dict) -> str:
        name = str(npc.get("name") or npc.get("display_name") or "Someone").strip()
        dist = ""
        try:
            dist_val = npc.get("dist", npc.get("distance", ""))
            if dist_val not in (None, ""):
                dist = f" ({int(float(dist_val))}m)"
        except Exception:
            pass
        return f"- {name}{dist}"

    def nearby_is_sapient(self, npc: dict) -> bool:
        category = str(npc.get("persona_category") or npc.get("category") or "").strip().lower()
        if category:
            return category == "sapient"
        race = str(npc.get("race") or npc.get("Race") or "").lower()
        non_sapient_bits = (
            "bonedog", "garru", "bull", "goat", "beak thing", "gorillo",
            "skimmer", "spider", "raptor", "crab", "leviathan", "wolf",
            "machine", "security spider", "cleanser unit",
        )
        return not any(bit in race for bit in non_sapient_bits)

    def nearby_is_player_squad(self, npc: dict, player_faction: str) -> bool:
        faction = str(npc.get("faction") or npc.get("Faction") or "").strip().lower()
        if not faction:
            return False
        return faction == "nameless" or faction.startswith("player's squad") or (player_faction and faction == player_faction)

    def nearby_is_injured(self, npc: dict) -> bool:
        health = str(npc.get("health") or npc.get("Health") or npc.get("condition") or "").strip().lower()
        if health and health not in ("healthy", "ok", "normal"):
            return True
        med = npc.get("medical")
        if isinstance(med, dict):
            return self.format_medical_condition(med, npc) != "healthy"
        return False

    def nearby_is_hostile(self, npc: dict) -> bool:
        if self.context_bool(npc, "is_hostile", "hostile"):
            return True
        relation = npc.get("relation", npc.get("Relation", ""))
        try:
            return float(relation) < 0
        except Exception:
            pass
        faction = str(npc.get("faction") or npc.get("Faction") or "").lower()
        hostile_bits = ("bandit", "cannibal", "fogmen", "fogman", "reaver", "slave traders", "skin bandit")
        return any(bit in faction for bit in hostile_bits)

    def context_bool(self, context: dict, *keys: str) -> bool:
        if not isinstance(context, dict):
            return False
        for key in keys:
            val = context.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return val != 0
            if isinstance(val, str) and val.strip().lower() in ("1", "true", "yes", "y"):
                return True
        return False

    def resolve_player_key_entities(self, ctx: TokenResolverContext, key_index: int, child_count: int) -> str:
        ent = self.resolve_player_keyword_entity(ctx, key_index)
        return self.render_entities(self.resolve_entity_chain(ent, child_count)) if ent is not None else ""

    def resolve_player_entity(self, ctx: TokenResolverContext, child_count: int) -> str:
        ent = self.resolve_player_entity_object(ctx)
        return self.render_entities(self.resolve_entity_chain(ent, child_count)) if ent is not None else ""

    def resolve_player_entity_object(self, ctx: TokenResolverContext):
        if not self.indexer:
            return None
        pc = ctx.player_context if isinstance(ctx.player_context, dict) else {}
        for key in ("persistent_id", "runtime_id", "id", "storage_id", "ID"):
            value = str(pc.get(key) or "").strip()
            if not value:
                continue
            try:
                uid = self.indexer.find_by_id(value)
            except Exception:
                uid = None
            if uid:
                ent = self.indexer.get(uid)
                if ent is not None:
                    return ent
        for name in (
            ctx.player_name,
            pc.get("name"),
            pc.get("player_name"),
            pc.get("display_name"),
        ):
            ent = self.resolve_entity_by_name(str(name or "").strip())
            if ent is not None:
                return ent
        return None

    def resolve_player_entity_field(self, ctx: TokenResolverContext, field_name: str) -> str:
        ent = self.resolve_player_entity_object(ctx)
        if ent is None:
            return ""
        fields = getattr(ent, "fields", {}) or {}
        for key in _PLAYER_ENTITY_FIELD_ALIASES.get(field_name, (field_name,)):
            val = fields.get(key)
            if val not in (None, ""):
                return str(val).strip()
        return ""

    def resolve_player_faction_entity(self, ctx: TokenResolverContext, child_count: int) -> str:
        faction_name = self.resolve_player_faction_name(ctx)
        return self.resolve_named_entity_text(faction_name, child_count)

    def resolve_player_faction_name(self, ctx: TokenResolverContext) -> str:
        faction_name = self.get_player_context_value(ctx, "player_faction")
        text = str(faction_name or "").strip()
        if not text:
            return ""
        return re.sub(r"^player'?s\s+squad\s*:\s*", "", text, flags=re.IGNORECASE).strip()

    def resolve_target_shop_stock(self, ctx: TokenResolverContext) -> str:
        stock = ctx.target_shop_stock
        if isinstance(stock, str):
            return stock.strip()
        if not isinstance(stock, list):
            return ""
        rows = [f"- {str(item).strip()}" for item in stock if str(item).strip()]
        return "\n".join(rows)

    def resolve_target_visible_gear(self, ctx: TokenResolverContext) -> str:
        meta = self.target_meta(ctx)
        inv_text = self.render_inventory_items(meta.get("inventory", []), None, equipped=True)
        if inv_text:
            return inv_text
        return str(meta.get("equipment") or meta.get("gear") or meta.get("Gear") or "").strip()

    def resolve_target_inventory_items(self, ctx: TokenResolverContext, count: int) -> str:
        meta = self.target_meta(ctx)
        inv_text = self.render_inventory_items(meta.get("inventory", []), count, equipped=False)
        if inv_text:
            return inv_text
        return str(meta.get("inventory_text") or "").strip()

    def resolve_target_stats(self, ctx: TokenResolverContext) -> str:
        return self.render_stats_block(self.target_meta(ctx))

    def render_stats_block(self, context: dict) -> str:
        if not isinstance(context, dict):
            return ""
        nested = context.get("stats")
        sources = []
        if isinstance(nested, dict):
            sources.append(nested)
        sources.append(context)
        rows = []
        for key in _STAT_KEYS:
            val = None
            for source in sources:
                if key in source and source.get(key) not in (None, ""):
                    val = source.get(key)
                    break
            if val not in (None, ""):
                label = key.replace("_", " ").title()
                rows.append(f"- {label}: {val}")
        return "\n".join(rows)

    def resolve_target_injuries(self, ctx: TokenResolverContext) -> str:
        meta = self.target_meta(ctx)
        rows = []
        condition = self.format_medical_condition({}, meta)
        if condition and condition != "healthy":
            rows.append(f"- Condition: {condition}")
        blood = self.format_blood_value(meta)
        if blood:
            rows.append(f"- Blood: {blood}")
        limbs = meta.get("limbs")
        if isinstance(limbs, dict):
            for limb, hp in limbs.items():
                if str(limb).endswith("_max"):
                    continue
                max_hp = limbs.get(f"{limb}_max", 100)
                try:
                    hp_f = float(hp)
                    max_f = float(max_hp or 100)
                    if hp_f <= -max_f:
                        rows.append(f"- {str(limb).replace('_', ' ').title()}: gone/severed")
                    elif hp_f < 0:
                        rows.append(f"- {str(limb).replace('_', ' ').title()}: crippled")
                    elif max_f > 0 and (hp_f / max_f) < 0.5:
                        rows.append(f"- {str(limb).replace('_', ' ').title()}: injured")
                except Exception:
                    continue
        return "\n".join(rows) if rows else "None"

    def resolve_target_traits(self, ctx: TokenResolverContext) -> str:
        meta = self.target_meta(ctx)
        traits = meta.get("traits") or meta.get("Traits")
        if isinstance(traits, dict):
            rows = [f"- {str(k).replace('_', ' ').title()}: {v}" for k, v in traits.items() if v not in (None, "")]
            return "\n".join(rows)
        if isinstance(traits, list):
            return "\n".join(f"- {x}" for x in traits if str(x).strip())
        parts = []
        for key in ("loyalty", "religion", "outlook", "motivation"):
            val = meta.get(key)
            if val not in (None, ""):
                parts.append(f"- {key.title()}: {val}")
        return "\n".join(parts) or str(traits or "").strip()

    def extract_player_keywords(self, ctx: TokenResolverContext) -> list[str]:
        if not self.indexer:
            return []
        try:
            keywords = extract_keywords(ctx.player_message or "", self.indexer, 10)
            return self.filter_current_location_keywords(ctx, keywords)
        except Exception:
            return []

    def resolve_player_keyword_entity(self, ctx: TokenResolverContext, key_index: int):
        entities = self.resolve_player_keyword_entities(ctx)
        if key_index < 1 or key_index > len(entities):
            return None
        return entities[key_index - 1]

    def resolve_player_keyword_entities(self, ctx: TokenResolverContext) -> list:
        cache_key = "__player_keyword_entities__"
        cached = self._prompt_cache.get(cache_key)
        if isinstance(cached, list):
            return cached
        excluded_uids = self.current_location_entity_uids(ctx)
        out = []
        seen = set()
        for keyword in self.extract_player_keywords(ctx):
            ent = self.resolve_entity_by_name(keyword)
            if ent is None:
                continue
            uid = str(getattr(ent, "uid", "") or "").strip()
            if not uid or uid in excluded_uids or uid in seen:
                continue
            seen.add(uid)
            out.append(ent)
        self._prompt_cache[cache_key] = out
        return out

    def resolve_entity_from_target_field(self, ctx: TokenResolverContext, field_names: tuple[str, ...], child_count: int) -> str:
        for f in field_names:
            val = self.resolve_target_metadata(ctx, f if f.startswith("target_") else f)
            if not val:
                val = (ctx.target_metadata or {}).get(f, "")
            if val:
                text = self.resolve_named_entity_text(str(val), child_count)
                if text:
                    return text
        return ""

    def resolve_speaker_entity(self, ctx: TokenResolverContext, speaker_index: int, fields: tuple[str, ...], child_count: int) -> str:
        speakers = ctx.radiant_speakers or []
        if speaker_index < 1 or speaker_index > len(speakers):
            return ""
        sp = speakers[speaker_index - 1] or {}
        for field in fields:
            val = str(sp.get(field) or "").strip()
            if val:
                text = self.resolve_named_entity_text(val, child_count)
                if text:
                    return text
        return ""

    def resolve_named_entity_text(self, name: Optional[str], child_count: int) -> str:
        ent = self.resolve_entity_by_name(name or "")
        if ent is None:
            return ""
        return self.render_entities(self.resolve_entity_chain(ent, child_count))

    def resolve_entity_by_name(self, name: str):
        if not self.indexer or not name or not self.is_valid_world_name(name):
            return None
        for candidate in (name, str(name).replace(" ", "_"), str(name).replace("'", "_")):
            uids = list(self.indexer.find_by_name(candidate) or [])
            if uids:
                return self.indexer.get(uids[0])
        kws = self.indexer.find_by_keyword(str(name))
        if kws:
            uid = sorted(kws)[0]
            return self.indexer.get(uid)
        return None

    def resolve_entity_chain(self, base_entity, child_count: int) -> list:
        if base_entity is None:
            return []
        out = [base_entity]
        out.extend(self.resolve_children_only_from_define_children(base_entity, child_count))
        return out

    def resolve_children_only_from_define_children(self, entity, child_count: int) -> list:
        if not entity or child_count <= 0 or not self.indexer:
            return []
        children = []
        for link in getattr(entity, "child_links", ())[:child_count]:
            cent = self.indexer.get(link.target_uid)
            if cent is not None:
                children.append(cent)
        return children

    def resolve_town_from_context(self, ctx: TokenResolverContext) -> Optional[str]:
        player_town = self.get_player_context_value(ctx, "player_town")
        if player_town and self.is_valid_world_name(player_town):
            return str(player_town)
        if ctx.server_town and self.is_valid_world_name(ctx.server_town):
            return ctx.server_town
        meta = ctx.target_metadata or {}
        for k in ("town_name", "town", "location", "city", "home_town"):
            v = meta.get(k)
            if v and self.is_valid_world_name(v):
                return str(v)
        return self.extract_latest_valid_town(self.get_world_synthesis_tail(ctx))

    def resolve_region_from_context(self, ctx: TokenResolverContext) -> Optional[str]:
        player_region = self.get_player_context_value(ctx, "player_region")
        if player_region and self.is_valid_world_name(player_region):
            return str(player_region)
        if ctx.server_region and self.is_valid_world_name(ctx.server_region):
            return ctx.server_region
        meta = ctx.target_metadata or {}
        for k in ("region", "home_region"):
            v = meta.get(k)
            if v and self.is_valid_world_name(v):
                return str(v)
        return self.extract_latest_valid_region(self.get_world_synthesis_tail(ctx))

    def resolve_random_entity_all(self, ctx: TokenResolverContext, child_count: int) -> str:
        return self._resolve_random_entity(ctx, None, child_count)

    def resolve_random_entity_folder(self, ctx: TokenResolverContext, folder_name: str, child_count: int) -> str:
        return self._resolve_random_entity(ctx, folder_name, child_count)

    def _resolve_random_entity(self, ctx: TokenResolverContext, folder_name: Optional[str], child_count: int) -> str:
        if not self.indexer:
            return ""
        ents = []
        wanted = (folder_name or "").strip().lower().replace(" ", "_")
        for ent in self.indexer.entities.values():
            cat = str(getattr(ent, "category", "") or "").lower()
            if cat in ("campaign_npcs", "base_npcs"):
                continue
            if wanted and cat != wanted:
                continue
            ents.append(ent)
        if not ents:
            return ""
        rng = random.Random(ctx.rng_seed) if ctx.rng_seed is not None else random
        ent = rng.choice(ents)
        return self.render_entities(self.resolve_entity_chain(ent, child_count))

    def world_events_file(self, ctx: TokenResolverContext) -> str:
        """Файл слухов кампании на стороне SentientSands.

        Отдельного параметра не заводим: путь к логу синтеза уже приходит, а
        оба файла лежат внутри server/ одной и той же копии мода.
        """
        explicit = str(getattr(ctx, "world_events_path", "") or "").strip()
        if explicit:
            return explicit
        synthesis = str(ctx.world_synthesis_path or "").strip()
        campaign = os.path.basename(str(ctx.campaign_root or "").rstrip("\\/"))
        if not synthesis or not campaign:
            return ""
        server_dir = os.path.dirname(os.path.dirname(synthesis))   # .../server
        return os.path.join(server_dir, "campaigns", campaign, "world_events.txt")

    def get_world_events_tail(self, ctx: TokenResolverContext, max_bytes: int = 32768) -> str:
        path = self.world_events_file(ctx)
        cache_key = f"__world_events_tail__:{path}:{max_bytes}"
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key]
        if not path or not os.path.isfile(path):
            self._prompt_cache[cache_key] = ""
            return ""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            data = ""
        self._prompt_cache[cache_key] = data
        return data

    def extract_rumor_entries(self, text: str, count: int, max_chars: int = 600) -> list[str]:
        """Слухи из world_events.txt: `- [Day N, HH:MM] [RUMOR: текст]`.

        Текст бывает многострочным и содержит вложенные скобки, поэтому
        закрывающую ищем по балансу, а не регулярным выражением.
        """
        if not text or count <= 0:
            return []
        marker = "[RUMOR:"
        found, pos = [], 0
        while True:
            start = text.find(marker, pos)
            if start < 0:
                break
            depth, i = 0, start
            while i < len(text):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            body = text[start + len(marker):i]
            pos = i + 1 if i < len(text) else len(text)

            # Модель иногда предваряет слух собственным заголовком.
            for lead in ("**Synthesized Rumor:**", "Synthesized Rumor:", "**Слух:**"):
                if lead in body:
                    body = body.split(lead, 1)[1]
            body = " ".join(body.split()).strip()
            # Слух — одно-три предложения. Простыня осталась от сбоя, когда
            # модель размышляла вслух прямо в ответ; такое в промпт не несём.
            if body and len(body) <= max_chars:
                found.append(body)

        seen, out = set(), []
        for rumor in reversed(found):
            key = rumor.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(rumor)
            if len(out) >= count:
                break
        return list(reversed(out))

    def get_world_synthesis_tail(self, ctx: TokenResolverContext, max_bytes: int = 32768) -> str:
        cache_key = f"__world_tail__:{ctx.world_synthesis_path}:{max_bytes}"
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key]
        path = ctx.world_synthesis_path or ""
        if not path:
            # Common local fallback: campaign logs first, then server-style sibling logs if present.
            cand = os.path.join(ctx.campaign_root or "", "logs", "world_synthesis.log")
            path = cand if os.path.isfile(cand) else ""
        if not path or not os.path.isfile(path):
            self._prompt_cache[cache_key] = ""
            return ""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                data = f.read().decode("utf-8", errors="replace")
            self._prompt_cache[cache_key] = data
            return data
        except Exception:
            self._prompt_cache[cache_key] = ""
            return ""

    def resolve_campaign_block(self, ctx: TokenResolverContext, field: str) -> str:
        if field == "campaign_chronicle":
            return str(ctx.campaign_chronicle or "").strip()
        if field == "world_synthesis":
            return str(ctx.world_synthesis or "").strip() or self.get_world_synthesis_tail(ctx)
        return ""

    def resolve_recent_events(self, ctx: TokenResolverContext, event_type: str, count: int) -> str:
        events = [str(e).strip() for e in (ctx.recent_events or []) if str(e).strip()]
        if not events or count <= 0:
            return ""
        if event_type == "local_events":
            events = self.filter_local_events(ctx, events)
        elif event_type == "combat_events":
            events = [e for e in events if self.event_is_combat(e)]
        elif event_type == "trade_events":
            events = [e for e in events if self.event_is_trade(e)]
        return "\n".join(f"- {e}" for e in events[-count:])

    def filter_local_events(self, ctx: TokenResolverContext, events: list[str]) -> list[str]:
        locs = [
            self.get_player_context_value(ctx, "player_town"),
            self.get_player_context_value(ctx, "player_region"),
            ctx.server_town,
            ctx.server_region,
        ]
        locs = [str(x).strip().lower() for x in locs if str(x or "").strip()]
        if not locs:
            return []
        return [e for e in events if any(loc in e.lower() for loc in locs)]

    def event_is_combat(self, event: str) -> bool:
        text = str(event or "").lower()
        keys = ("[combat]", "[knockout]", "[death]", "[dead]", " attack", " attacked", " killed", "bleed", "battle", "violence")
        return any(k in text for k in keys)

    def event_is_trade(self, event: str) -> bool:
        text = str(event or "").lower()
        keys = ("[trade]", "[player-trade]", " bought ", " sold ", "trade", "trader", "payment", "cats")
        return any(k in text for k in keys)

    def extract_recent_rumors(self, tail_text: str, count: int) -> list[str]:
        if not tail_text or count <= 0:
            return []
        rumors = []
        lines = [l.strip() for l in tail_text.splitlines()]
        # Prefer actual LLM output after MESSAGE 2 in prompt snapshots.
        for i, line in enumerate(lines):
            if line.startswith("RAW LLM RESPONSE:") and i + 1 < len(lines):
                txt = lines[i + 1].strip()
                if txt:
                    rumors.append(txt)
        # Fallback: previous rumor list styles.
        for line in lines:
            if re.match(r"^(?:[-*]|\d+[.)])\s+", line) and len(line) > 12:
                rumors.append(re.sub(r"^(?:[-*]|\d+[.)])\s+", "", line).strip())
        seen, out = set(), []
        for rumor in reversed(rumors):
            key = rumor.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(rumor)
            if len(out) >= count:
                break
        return list(reversed(out))

    def extract_latest_violence_summary(self, tail_text: str) -> Optional[str]:
        for line in reversed((tail_text or "").splitlines()):
            s = line.strip()
            if s.startswith("Violence summary:"):
                return s
        return None

    def extract_latest_trade_summary(self, tail_text: str) -> Optional[str]:
        for line in reversed((tail_text or "").splitlines()):
            s = line.strip()
            if s.startswith("Trade summary:"):
                return s
        return None

    def extract_latest_valid_town(self, tail_text: str) -> Optional[str]:
        for line in reversed((tail_text or "").splitlines()):
            m = re.match(r"@\s+(.+?)\s*$", line.strip())
            if m:
                val = m.group(1).strip()
                if self.is_valid_world_name(val):
                    return val
        # Also support player-summary location lines.
        for line in reversed((tail_text or "").splitlines()):
            m = re.search(r"Locations:\s*([^\.\n]+)", line)
            if m:
                for val in re.split(r",|\band\b", m.group(1)):
                    val = val.strip()
                    if self.is_valid_world_name(val):
                        return val
        return None

    def extract_latest_valid_region(self, tail_text: str) -> Optional[str]:
        # region is not reliably explicit in current synthesis logs; leave this conservative.
        return None

    def extract_latest_active_faction(self, tail_text: str, exclude: set[str] | None = None) -> Optional[str]:
        exclude_norm = {str(x or "").strip().lower() for x in (exclude or set()) if str(x or "").strip()}
        summary = self.extract_latest_violence_summary(tail_text) or ""
        candidates = []
        # Pull capitalized faction-like phrases from the latest violence line.
        for phrase in re.findall(r"[A-Z][A-Za-z'\-]*(?:\s+[A-Z][A-Za-z'\-]*){0,3}", summary):
            clean = phrase.strip(" .,:;()[]")
            if clean.lower() in exclude_norm or not self.is_valid_world_name(clean):
                continue
            if clean in ("Violence", "Trade", "Summary"):
                continue
            candidates.append(clean)
        for cand in candidates:
            if self.resolve_entity_by_name(cand) is not None:
                return cand
        return None

    def render_entities(self, entities: list) -> str:
        blocks = [self.render_single_entity(e) for e in (entities or []) if e is not None]
        return "\n\n---\n\n".join(b for b in blocks if b.strip())

    def render_single_entity(self, entity) -> str:
        if entity is None:
            return ""
        try:
            return self.prompt_builder.render_entity(entity)
        except Exception:
            return ""

    def render_location_entity_excerpt(self, label: str, entity, fallback_name: str = "") -> str:
        if entity is None:
            return ""
        lines = [f"[{label}]"]
        name = str(getattr(entity, "display_name", "") or fallback_name or "").strip()
        category = str(getattr(entity, "category", "") or "").strip()
        best_id = str(getattr(entity, "best_id", "") or "").strip()

        if name:
            lines.append(f"- Name: {name}")
        if category:
            lines.append(f"- Category: {category}")
        if best_id:
            lines.append(f"- Id: {best_id}")

        for key, nice in _LOCATION_STRUCT_FIELDS:
            value = self.entity_field_value(entity, key)
            if value:
                lines.append(f"- {nice}: {value}")

        for key, nice in _LOCATION_PROSE_FIELDS:
            value = self.entity_field_value(entity, key)
            if value:
                lines.append(f"- {nice}: {value}")

        return "\n".join(lines)

    def entity_field_value(self, entity, *keys: str) -> str:
        fields = getattr(entity, "fields", {}) if entity is not None else {}
        if not isinstance(fields, dict):
            return ""
        for key in keys:
            clean_key = str(key or "").strip()
            if not clean_key:
                continue
            candidates = (clean_key, f"${clean_key}", clean_key.lstrip("$"), f"${clean_key.lstrip('$')}")
            for cand in candidates:
                value = fields.get(cand)
                if value not in (None, ""):
                    return str(value).strip()
            wanted = clean_key.lower().lstrip("$")
            for field_key, value in fields.items():
                if str(field_key).lower().lstrip("$") == wanted and value not in (None, ""):
                    return str(value).strip()
        return ""

    def render_rumors(self, rumors: list[str]) -> str:
        clean = [str(r).strip() for r in (rumors or []) if str(r).strip()]
        if not clean:
            return ""
        if len(clean) == 1:
            return clean[0]
        return "\n".join(f"{i}. {r}" for i, r in enumerate(clean, start=1))

    def render_plain_text_block(self, text: str) -> str:
        return str(text or "").strip()

    def clean_replacement_text(self, text: str) -> str:
        return str(text or "").strip()

    def normalize_token_whitespace(self, raw_token: str) -> str:
        return re.sub(r"\s+", " ", str(raw_token or "").strip())

    def is_valid_world_name(self, name: str) -> bool:
        n = str(name or "").strip()
        return bool(n) and n.lower() not in _INVALID_WORLD_NAMES

    def make_cache_key(self, token: ParsedToken, ctx: TokenResolverContext) -> str:
        return token.raw

    def log_token_hit(self, token: ParsedToken, result_preview: str = "") -> None:
        if self.logger:
            try:
                self.logger.debug(f"TOKEN HIT: {token.raw} -> {result_preview}")
            except Exception:
                pass

    def log_token_miss(self, token: ParsedToken, reason: str) -> None:
        if self.logger:
            try:
                self.logger.debug(f"TOKEN MISS: {token.raw} -> {reason}")
            except Exception:
                pass
