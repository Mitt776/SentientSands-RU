# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak ↔ SentientSands Bridge — Data Translation & Business Logic Layer.

This bridge owns all SentientSands-specific decision-making:
  • Faction-based knowledge seeding
  • Action hint detection and injection
  • NPC weight calculations (based on relation)
  • Pricing system (global/city/NPC modifiers)
  • Dialogue persistence and knowledge growth
  • Field name translation (SS → Kayak)

HTTP communication is delegated to hub.py.
This bridge focuses purely on WHAT to write/read and HOW to transform it.
"""

import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from .hub import KayakHub
from ..kayak_keys import Ops

try:
    # Живёт на стороне SentientSands; при отдельном запуске Kayak его нет,
    # и перевод имён просто отключается.
    import game_locale as _game_locale
except Exception:  # pragma: no cover - зависит от того, кто запустил процесс
    _game_locale = None

log = logging.getLogger("kayak.ss.bridge")


# ─── FACTION KNOWLEDGE TABLE ──────────────────────────────────────────────────

def _knowledge_key(name) -> str:
    """Ключ таблиц знаний: без регистра, артикля, апострофов и подчёркиваний."""
    text = str(name or "").strip().lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[\s_-]+", " ", text).strip()
    return re.sub(r"^the\s+", "", text)


_FACTION_KNOWLEDGE: dict = {
    "anti slavers": {
        "Anti_Slavers", "Manhunters", "Slave_Traders", "Spring", "Stobe_s_Gamble",
        "Traders_Guild", "United_Cities",
    },
    "band of bones": {"Band_of_Bones", "Stenn_Desert", "The_Hook"},
    "berserkers": {"Berserkers", "Stenn_Desert", "Stobe_s_Gamble"},
    "cannibals": {"Cannibal_Plains", "Cannibals", "Darkfinger"},
    "dark hive": {"Dark_Hive"},
    "drifters": {"Drifters"},
    "dust bandits": {"Border_Zone", "Dust_Bandits", "Great_Desert"},
    "flotsam ninjas": {"Cannibal_Plains", "Flotsam_Ninjas", "Flotsam_Village", "Holy_Nation"},
    "fogmen": {"Fog_Islands", "Fogmen"},
    "holy nation": {
        "Anti_Slavers", "Arm_of_Okran", "Bad_Teeth", "Blister_Hill", "Flotsam_Ninjas",
        "Holy_Farms", "Holy_Lands", "Holy_Military_Bases", "Holy_Mines", "Holy_Nation",
        "Holy_Phoenix", "Narko_s_Trap", "Okran_and_Narko", "Okran_s_Fist", "Okran_s_Gulf",
        "Okran_s_Pride", "Okran_s_Shield", "Okran_s_Valley", "Rebirth", "Shek_Kingdom",
        "Skinner_s_Roam", "Stack", "Tech_Hunters", "The_Okranite_Rebellion",
        "The_Slave_Economy", "The_Thousand_Year_War", "United_Cities", "Worlds_End",
    },
    "holy nation outlaws": {"Holy_Nation_Outlaws", "The_Hub"},
    "hounds": {"Hounds"},
    "kral s chosen": {"Kral_s_Chosen", "Stenn_Desert", "The_Hook"},
    "manhunters": {"Manhunters"},
    "mercenary guild": {"Mercenary_Guild"},
    "nameless": {"Nameless"},
    "nomads": {"Border_Zone", "Great_Desert", "Nomads"},
    "reavers": {"Reavers", "South_Wetlands", "The_Hook"},
    "second empire exiles": {"Greyshelf", "Second_Empire_Exiles"},
    "shek kingdom": {
        "Admag", "Band_of_Bones", "Berserkers", "Border_Zone", "Esata_the_Stone_Golem",
        "Holy_Nation", "Kral_s_Chosen", "Last_Stand", "Okran_s_Gulf", "Shek_Kingdom",
        "Squin", "Stenn_Desert", "The_Great_Fortress", "The_Hook",
        "The_Shek_Extinction_Crisis", "The_Thousand_Year_War", "United_Cities",
    },
    "shinobi thieves": {"Mongrel", "Shinobi_Thieves"},
    "skin bandits": {"Skin_Bandits"},
    "slave traders": {"Anti_Slavers", "Eyesocket", "Slave_Traders", "Stone_Camp"},
    "tech hunters": {"Black_Scratch", "Flats_Lagoon", "Mourn", "Tech_Hunters", "Worlds_End"},
    "trade ninjas": {"Trade_Ninjas"},
    "traders guild": {
        "Anti_Slavers", "Great_Desert", "Heft", "Trader_s_Edge", "Traders_Guild",
        "United_Cities",
    },
    "united cities": {
        "Anti_Slavers", "Bark", "Black_Scratch", "Bonefields", "Border_Zone", "Brink",
        "Brink_area", "Catun", "Clownsteady", "Drifters_Last", "Emperor_Tengu", "Eyesocket",
        "Flats_Lagoon", "Flotsam_Ninjas", "Great_Desert", "Heft", "Heng", "Holy_Nation",
        "Longen", "Manhunters", "Port_North", "Port_South", "Shek_Kingdom", "Sho_Battai",
        "Sinkuun", "Skimsands", "Slave_Traders", "South_Wetlands", "Stoat", "Stone_Camp",
        "Stormgap_Coast", "Tech_Hunters", "The_Hook", "The_Hub", "The_Red_Rebellion",
        "The_Slave_Economy", "Trader_s_Edge", "Traders_Guild", "United_Cities",
    },
    "vagrants": {"Vagrants"},
    "western hive": {"Dreg", "Hive_Village", "Vain", "Western_Hive"},
    "^default": {"The_Hub", "Border_Zone"},
}

# Русские написания из display_name сущностей базы и объявленных aliases.
_FACTION_ALIASES: dict = {
    "krals chosen": "kral s chosen",
    "безымянные": "nameless",
    "берсерки": "berserkers",
    "бродяги": "vagrants",
    "воры шиноби": "shinobi thieves",
    "гильдия наёмников": "mercenary guild",
    "гильдия торговцев": "traders guild",
    "гончие": "hounds",
    "груда костей": "band of bones",
    "западный улей": "western hive",
    "избранный крала": "kral s chosen",
    "изгнанники второй империи": "second empire exiles",
    "изгои святой нации": "holy nation outlaws",
    "кожаные бандиты": "skin bandits",
    "королевство шеков": "shek kingdom",
    "кочевники": "nomads",
    "людоеды": "cannibals",
    "ниндзя отщепенцы": "flotsam ninjas",
    "ниндзя торговцы": "trade ninjas",
    "охотники на людей": "manhunters",
    "противники рабства": "anti slavers",
    "пыльные бандиты": "dust bandits",
    "рабовладельцы": "slave traders",
    "разбойники": "reavers",
    "святая нация": "holy nation",
    "скитальцы": "drifters",
    "союзные города": "united cities",
    "техохотники": "tech hunters",
    "туманники": "fogmen",
    "тёмный улей": "dark hive",
}

# Значения — как и у фракций, имена папок сущностей базы.
_RACE_KNOWLEDGE: dict = {
    "greenlander":  {"Greenlander", "Holy_Nation", "United_Cities", "Border_Zone", "The_Hub"},
    "scorchlander": {"Scorchlander", "United_Cities", "Great_Desert", "Border_Zone", "The_Hub"},
    "shek":         {"Shek", "Shek_Kingdom", "Stenn_Desert", "Admag", "Squin"},
    "hiver":        {"Vain", "Hive_Village", "Western_Hive", "United_Cities", "The_Hub"},
    "skeleton":     {"Skeleton", "Black_Desert", "Worlds_End", "Ancient_Labs", "The_Hub"},
}

# Русские написания из display_name сущностей рас. Три касты Улья ведут к
# общему набору: место обитания у них одно, различает их сама сущность расы.
_RACE_ALIASES: dict = {
    "зеленоземец": "greenlander",
    "жженоземец": "scorchlander",
    "шек": "shek",
    "скелет": "skeleton",
    "принц": "hiver",
    "солдат дрон": "hiver",
    "рабочий дрон": "hiver",
    "hiver prince": "hiver",
    "hiver soldier drone": "hiver",
    "hiver worker drone": "hiver",
}


def _canonical_key(raw, aliases: dict) -> str:
    """Привести имя к ключу таблицы: как есть, через псевдоним, через игру."""
    key = _knowledge_key(raw)
    if not key:
        return ""
    if key in aliases:
        return aliases[key]
    if _game_locale is not None:
        # Имени нет в базе — спросим локализацию самой Kenshi.
        english = _knowledge_key(_game_locale.to_english_key(raw))
        if english and english != key:
            return aliases.get(english, english)
    return key


def _faction_seed(raw) -> set:
    key = _canonical_key(raw, _FACTION_ALIASES)
    return set(_FACTION_KNOWLEDGE.get(key, ()))


def _race_seed(raw) -> set:
    key = _canonical_key(raw, _RACE_ALIASES)
    return set(_RACE_KNOWLEDGE.get(key, ()))


def _build_initial_knowledge(faction: str, origin_faction: str = "") -> set:
    """Seed NPC's $knows_about field based on faction."""
    seed = _faction_seed(faction)
    if not seed:
        seed = _faction_seed(origin_faction)
    if not seed:
        seed = set(_FACTION_KNOWLEDGE.get("^default", set()))
    if origin_faction and _knowledge_key(origin_faction) != _knowledge_key(faction):
        seed |= _faction_seed(origin_faction)
    return seed


# ─── MERGE-MODE ENTITY RECONSTRUCTION ────────────────────────────────────────
#
# When updating an existing entity, we merge incoming fields on top of existing
# ones and preserve any custom/manual fields the user added. This protects
# hand-edited entity.txt files from being overwritten.

# Prose fields get $ prefix in entity.txt. Kayak strips $ on read, so we need
# to know which fields to re-prefix when reconstructing.
_PROSE_FIELDS = frozenset({"personality", "backstory", "speech_quirks", "knows_about"})

# Fields managed by the bridge. Everything else is "custom" and preserved as-is.
_HEADER_FIELDS = ("category", "name", "id")
_ID_FIELDS = ("persistent_id", "runtime_id")
_STRUCTURAL_FIELDS = (
    "display_name", "original_name", "race", "sex", "faction", "origin_faction",
    "role", "weight", "relation", "persona_category",
)
_INTERNAL_FIELDS = ("profile_state", "has_dialogue")
_TRAIT_FIELDS_ENTITY = ("loyalty", "religion", "outlook", "motivation")
_ALL_MANAGED = (
    frozenset(_HEADER_FIELDS)
    | frozenset(_ID_FIELDS)
    | frozenset(_STRUCTURAL_FIELDS)
    | frozenset(_INTERNAL_FIELDS)
    | frozenset(_TRAIT_FIELDS_ENTITY)
    | _PROSE_FIELDS
)


def _merge_fields(existing: Dict[str, str], incoming: Dict[str, str]) -> Dict[str, str]:
    """
    Merge incoming fields onto existing, preserving custom fields.

    Rules:
      - Incoming non-empty string → replaces existing
      - Incoming empty string → keeps existing value (don't clear by accident)
      - Field in existing but not in incoming → preserved (custom fields survive)
    """
    merged = dict(existing)
    for key, val in incoming.items():
        if key.startswith("_"):
            continue  # skip metadata like _entity_name
        val_str = str(val).strip()
        if val_str:
            merged[key] = val_str
        # If val_str is empty, we keep whatever was in existing (or nothing)
    return merged


def _reconstruct_entity_content(fields: Dict[str, str]) -> str:
    """
    Reconstruct entity.txt content from a merged fields dict.

    Output order:
      1. Header: Category, Name, Id (+ blank line)
      2. IDs: persistent_id, runtime_id
      3. Structural: display_name through relation
      4. Knowledge: $knows_about
      5. Traits: loyalty, religion, outlook, motivation
      6. Prose: $personality, $backstory, $speech_quirks
      7. Custom: any remaining fields not in the managed set
    """
    lines: list = []
    emitted: set = set()

    def _emit(key: str) -> None:
        val = str(fields.get(key, "")).strip()
        if not val:
            return
        emitted.add(key)
        prefix = "$" if key in _PROSE_FIELDS else ""
        lines.append(f"{prefix}{key} = {val}")

    # Header
    lines.append(f"Category = {fields.get('category', 'campaign_npcs')}")
    emitted.add("category")
    lines.append(f"Name = {fields.get('name', '')}")
    emitted.add("name")
    lines.append(f"Id = {fields.get('id', '')}")
    emitted.add("id")
    lines.append("")

    # IDs
    for key in _ID_FIELDS:
        _emit(key)

    # Structural
    for key in _STRUCTURAL_FIELDS:
        _emit(key)

    # Internal lifecycle fields
    for key in _INTERNAL_FIELDS:
        _emit(key)

    # Knowledge
    _emit("knows_about")

    # Traits
    for key in _TRAIT_FIELDS_ENTITY:
        _emit(key)

    # Prose
    for key in ("personality", "backstory", "speech_quirks"):
        _emit(key)

    # Custom / unknown fields — everything not yet emitted
    for key, val in fields.items():
        if key in emitted or key.startswith("_"):
            continue
        val_str = str(val).strip()
        if not val_str:
            continue
        # Custom fields get $ prefix by convention (safe default for content)
        prefix = "$" if key not in _ALL_MANAGED else ""
        lines.append(f"{prefix}{key} = {val_str}")

    return "\n".join(lines)


# ─── SENTIENT SANDS BRIDGE ────────────────────────────────────────────────────

class SentientSandsBridge:
    """
    Bridge between SentientSands and Kayak.
    Owns all SS-specific logic; delegates HTTP to hub.
    """

    def __init__(
        self,
        kayak_url: str = "http://127.0.0.1:5001",
        campaign: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.kayak_url = kayak_url
        self.campaign = campaign
        self.hub = KayakHub(kayak_url, timeout)
        # Prevent repeated WRITE_PROFILE saves from re-appending the same
        # dialogue exchange when older Kayak builds lack /dialogue/replace.
        self._last_dialogue_sync_sig: Dict[str, str] = {}
        self.sentient_songs = None
        try:
            from ..SentientSongs import SentientSongsClient

            self.sentient_songs = SentientSongsClient(controller_name="kayak", auto_start=True)
            _songs_ok, _songs_msg = self.sentient_songs.start_heartbeat()
            if not _songs_ok:
                log.warning(f"SentientSongs heartbeat startup failed: {_songs_msg}")
        except Exception as exc:
            log.warning(f"SentientSongs client unavailable: {exc}")

    def is_alive(self, force: bool = False) -> bool:
        """Proxy for hub.is_alive."""
        return self.hub.is_alive(force=force)

    @property
    def _circuit_broken(self) -> bool:
        """Proxy for hub._circuit_broken."""
        return getattr(self.hub, "_circuit_broken", False)

    # ─── CAMPAIGN ─────────────────────────────────────────────────────────

    def on_save_loaded(self, campaign_name: str, ss_campaign_dir: Optional[str] = None):
        """Call every time a SentientSands campaign is activated."""
        if not self.hub.switch_campaign(campaign_name):
            log.error(f"Failed to switch Kayak to campaign '{campaign_name}'")
            return
        self.campaign = campaign_name
        # Dialogue signature cache is per-campaign. Reset on campaign switch so
        # first writes in a fresh campaign are never suppressed.
        self._last_dialogue_sync_sig.clear()
        if ss_campaign_dir:
            self.sync_player_bio(ss_campaign_dir, campaign_name)
        log.info(f"Campaign switched to '{campaign_name}'")

    # Заголовок для 5_player_bio.txt. Файл попадает в промпт вплотную к
    # разговору, поэтому обязан выглядеть справкой, а не репликой.
    _PLAYER_BIO_HEADER = (
        "<H3>КАК ИГРОК ОПИСЫВАЕТ СЕБЯ САМ</H3>\n\n"
        "Это справка о собеседнике, а не его слова и не часть разговора выше.\n"
        "Никогда не повторяй и не пересказывай этот текст в своём ответе.\n\n"
    )

    def sync_player_bio(self, ss_campaign_dir: str, campaign: Optional[str] = None):
        """Sync player bio from SentientSands campaign folder to Kayak."""
        bio_path = os.path.join(ss_campaign_dir, "character_bio.txt")
        fac_path = os.path.join(ss_campaign_dir, "player_faction_description.txt")

        bio_text = _read_text(bio_path)
        fac_text = _read_text(fac_path)

        if not bio_text and not fac_text:
            return

        parts = []
        if bio_text:
            parts.append(bio_text.strip())
        if fac_text and fac_text.strip():
            parts.append(fac_text.strip())

        # Файл кладётся в промпт последним — между историей реплик и
        # PLAYER_MESSAGE. Без заголовка модель читает его как ещё одну реплику
        # и однажды выдала это био дословно вместо ответа NPC.
        content = self._PLAYER_BIO_HEADER + "\n\n".join(parts)

        payload = {
            "filename": "5_player_bio.txt",
            "content": content,
            "scope": "campaign",
            "prompt_type": "Chat",
        }
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        ok = self.hub.write_mandatory_file(payload)
        if ok:
            log.info(f"Player bio synced for campaign '{campaign or self.campaign}'")
        else:
            log.warning(f"Failed to sync player bio for '{campaign or self.campaign}'")

    # ─── WRITE: NPC PROFILE ────────────────────────────────────────────

    def write_npc_profile(self, char_data: Dict[str, Any], campaign: Optional[str] = None):
        """Write or update an NPC profile as entity.txt in Kayak.

        MERGE MODE: If an entity already exists, incoming fields are merged
        on top of existing ones. Custom/manual fields in entity.txt are
        preserved. Empty incoming fields do not overwrite existing values.

        First-time writes build entity.txt from scratch.
        """
        name = _clean_name(char_data.get("Name") or char_data.get("name") or "")
        if not name:
            return False

        requested_folder = _to_folder_name(name)
        persistent_id = str(
            char_data.get("ID")
            or char_data.get("persistent_id")
            or ""
        ).strip()
        runtime_id = str(
            char_data.get("runtime_id")
            or char_data.get("id")
            or ""
        ).strip()

        # Extract incoming fields from the SS dict
        incoming = {
            "display_name":    name,
            "race":            str(char_data.get("Race") or "").strip(),
            "sex":             str(char_data.get("Sex") or "").strip(),
            "faction":         str(char_data.get("Faction") or "").strip(),
            "origin_faction":  str(char_data.get("OriginFaction") or "").strip(),
            "role":            str(char_data.get("Job") or "").strip(),
            "personality":     str(char_data.get("Personality") or "").strip(),
            "backstory":       str(char_data.get("Backstory") or "").strip(),
            "speech_quirks":   str(char_data.get("SpeechQuirks") or "").strip(),
        }
        persona_category = str(
            char_data.get("persona_category")
            or char_data.get("_persona_category")
            or ""
        ).strip()
        if persona_category:
            incoming["persona_category"] = persona_category
        if persistent_id:
            incoming["persistent_id"] = persistent_id
        if runtime_id:
            incoming["runtime_id"] = runtime_id

        profile_state = str(char_data.get("_profile_state") or "").strip()
        has_dialogue = str(char_data.get("_has_dialogue") or "").strip()
        explicit_original = str(
            char_data.get("_original_name")
            or char_data.get("original_name")
            or ""
        ).strip()
        if profile_state:
            incoming["profile_state"] = profile_state
        if has_dialogue:
            incoming["has_dialogue"] = has_dialogue

        # Relation + weight
        relation = char_data.get("Relation", 0)
        incoming["relation"] = str(relation)
        try:
            weight = 5 + max(-3, min(3, int(relation) // 20))
        except (ValueError, TypeError):
            weight = 5
        incoming["weight"] = str(weight)

        # Traits
        traits = char_data.get("Traits") or {}
        for trait_key in ("Loyalty", "Religion", "Outlook", "Motivation"):
            val = str(traits.get(trait_key) or "").strip()
            if val:
                incoming[trait_key.lower()] = val

        # Read existing entity (for merge + folder preservation)
        existing_fields = self.get_npc_fields(
            requested_folder,
            persistent_id or runtime_id or None,
            campaign or self.campaign,
        )

        # Preserve entity folder when NPC was renamed in-game
        existing_folder = _to_folder_name(existing_fields.get("_entity_name", ""))
        folder_name = existing_folder or requested_folder
        existing_category = str(
            existing_fields.get("category")
            or existing_fields.get("_entity_category")
            or ""
        ).strip().lower()
        category_name = existing_category if existing_category in {"campaign_npcs", "base_npcs"} else "campaign_npcs"
        if existing_folder and existing_folder != requested_folder:
            log.info(
                f"write_npc_profile: preserving entity folder '{existing_folder}' "
                f"for renamed NPC '{name}'"
            )

        # original_name tracking: set once, never overwritten.
        # This is metadata only. It never drives display_name or folder renames.
        existing_original = str(existing_fields.get("original_name") or "").strip()
        if explicit_original:
            incoming["original_name"] = explicit_original
        elif existing_original:
            # Already set — preserve it, don't overwrite.
            incoming["original_name"] = existing_original
        else:
            # First observation — record the current name as original.
            incoming["original_name"] = name

        # Knowledge merge (always additive)
        faction_str = incoming.get("faction", "")
        origin_str = incoming.get("origin_faction", "")
        existing_knows = existing_fields.get("knows_about", "").strip()
        seed_knowledge = _build_initial_knowledge(faction_str, origin_str)
        if existing_knows:
            existing_set = {x.strip() for x in existing_knows.split(",") if x.strip()}
            merged_knows = existing_set | seed_knowledge
        else:
            merged_knows = seed_knowledge
        if merged_knows:
            incoming["knows_about"] = ", ".join(sorted(merged_knows))

        # Set header fields
        incoming["category"] = category_name
        incoming["name"] = folder_name
        incoming["id"] = persistent_id or runtime_id

        # MERGE or BUILD
        if existing_fields:
            merged = _merge_fields(existing_fields, incoming)
        else:
            merged = incoming

        content = _reconstruct_entity_content(merged)

        payload = {
            "category": category_name,
            "name": folder_name,
            "entity_content": content,
        }
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        ok = self.hub.write_npc_profile(payload)
        if not ok:
            log.warning(f"Failed to write NPC profile for {name}")
        return ok

    def ensure_npc_identity(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        live_ctx: Optional[Dict[str, Any]] = None,
        campaign: Optional[str] = None,
    ) -> dict:
        """
        Persist newly observed runtime/persistent IDs onto existing base_npcs
        entities before dialogue/profile systems key off a stale placeholder ID.
        """
        active_campaign = campaign or self.campaign
        fields = dict(self.get_npc_fields(target_npc, target_npc_id, active_campaign) or {})
        if not fields:
            return {}

        category = str(
            fields.get("_entity_category")
            or fields.get("category")
            or ""
        ).strip().lower()
        if category != "base_npcs":
            return fields

        context = live_ctx if isinstance(live_ctx, dict) else {}
        desired_pid = str(context.get("persistent_id") or "").strip()
        desired_rid = str(context.get("runtime_id") or context.get("id") or "").strip()
        desired_id = desired_pid or desired_rid

        current_pid = str(fields.get("persistent_id") or "").strip()
        current_rid = str(fields.get("runtime_id") or "").strip()
        current_id = str(fields.get("id") or "").strip()

        updates = []
        if desired_pid and desired_pid != current_pid:
            updates.append(("persistent_id", desired_pid))
        if desired_rid and desired_rid != current_rid:
            updates.append(("runtime_id", desired_rid))
        if desired_id and desired_id != current_id:
            updates.append(("id", desired_id))
        if not updates:
            return fields

        entity_name = str(fields.get("_entity_name") or _to_folder_name(target_npc) or "").strip()
        anchor_id = current_pid or current_rid or str(target_npc_id or "").strip()
        wrote_any = False

        for field_name, field_value in updates:
            payload: Dict[str, Any] = {
                "field": field_name,
                "value": field_value,
            }
            if entity_name:
                payload["target_npc"] = entity_name
            elif target_npc:
                payload["target_npc"] = _to_folder_name(target_npc)
            if anchor_id:
                payload["target_npc_id"] = anchor_id
            if active_campaign:
                payload["campaign"] = active_campaign
            if self.hub.write_entity_field(payload):
                wrote_any = True

        if wrote_any:
            log.info(
                f"ensure_npc_identity: bound base NPC '{target_npc}' "
                f"(pid={desired_pid or '-'}, rid={desired_rid or '-'})"
            )
            refreshed = self.get_npc_fields(
                target_npc,
                desired_pid or desired_rid or target_npc_id,
                active_campaign,
            )
            if refreshed:
                return dict(refreshed)

        return fields

    # ─── WRITE: STATS ──────────────────────────────────────────────────

    def write_stats_from_context(
        self,
        npc_name: str,
        live_ctx: Dict[str, Any],
        campaign: Optional[str] = None,
        shop_stock: Optional[List[str]] = None,
    ):
        """Write live stats to stats.txt (no reindex — read fresh at prompt-time)."""
        if not live_ctx:
            return

        lines = [f"NPC: {npc_name}"]

        state = live_ctx.get("character_state", "normal")
        if state != "normal":
            lines.append(f"state: {state}")

        for key in ("race", "faction", "job", "money"):
            val = live_ctx.get(key)
            if val is not None:
                lines.append(f"{key}: {val}")

        med = live_ctx.get("medical", {})
        if med:
            blood = med.get("blood", 100)
            max_b = med.get("max_blood", 100)
            hunger = med.get("hunger", 300)
            bpct = int(blood / max_b * 100) if max_b > 0 else 100
            lines.append(f"blood: {bpct}%")
            if hunger < 100:
                lines.append("hunger: starving")
            elif hunger < 250:
                lines.append("hunger: hungry")

        env = live_ctx.get("environment", {})
        town = ""
        if isinstance(env, dict):
            town = env.get("town_name", "")
            if town:
                lines.append(f"location: {town}")

        relation = live_ctx.get("relation")
        if relation is not None:
            lines.append(f"relation_to_player: {relation}")

        inventory = live_ctx.get("inventory", [])
        if not isinstance(inventory, list):
            inventory = []

        def _render_item(item: Dict[str, Any], include_slot: bool = False) -> str:
            name = str(item.get("name") or "Unknown Item").strip() or "Unknown Item"
            try:
                count = int(item.get("count", 1))
            except (TypeError, ValueError):
                count = 1
            bits = [f"{name} (x{count})"]
            if include_slot:
                slot = str(item.get("slot") or "").strip()
                if slot:
                    bits.append(f"[{slot.upper()}]")
            price = item.get("price")
            if price is not None:
                bits.append(f"[value: {price} cats]")
            return " ".join(bits)

        worn_inv = [item for item in inventory if item.get("equipped") and item.get("name")]
        held_inv = [item for item in inventory if not item.get("equipped") and item.get("name")]

        if worn_inv:
            lines.append("[VISIBLE GEAR] Equipped or visibly worn items:")
            for item in worn_inv:
                lines.append(f"  - {_render_item(item, include_slot=True)}")

        if held_inv:
            held_limit = 25 if live_ctx.get("is_trader", False) else 10
            if live_ctx.get("is_trader", False):
                lines.append("[PERSONAL INVENTORY] Carried on the NPC's person. This is not the shop stock list unless repeated below:")
            else:
                lines.append("[PERSONAL INVENTORY] Items currently carried:")
            for item in held_inv[:held_limit]:
                lines.append(f"  - {_render_item(item)}")
            if len(held_inv) > held_limit:
                lines.append(f"  - ... and {len(held_inv) - held_limit} more items.")

        # Pricing block for traders/shopkeepers
        # Trust only the DLL trader flag for merchant behavior.
        _is_trader = live_ctx.get("is_trader", False)
        _in_shop = live_ctx.get("in_shop", False)

        if _is_trader:
            g_mod = self._get_global_price_modifier()
            c_mod = self._get_city_price_modifier(town, campaign)
            npc_fields = self.get_npc_fields(npc_name, None, campaign)
            npc_pm_raw = npc_fields.get("price_modifier", "")
            try:
                n_mod = float(npc_pm_raw) if npc_pm_raw else 1.0
            except (ValueError, TypeError):
                n_mod = 1.0

            eff = round(g_mod * c_mod * n_mod, 4)
            if eff == 1.0:
                price_note = "Prices are at standard base value."
            elif eff > 1.0:
                pct = round((eff - 1.0) * 100)
                price_note = (
                    f"Prices are {pct}% above base "
                    f"(global ×{g_mod}, city ×{c_mod}, npc ×{n_mod})."
                )
            else:
                pct = round((1.0 - eff) * 100)
                price_note = (
                    f"Prices are {pct}% below base "
                    f"(global ×{g_mod}, city ×{c_mod}, npc ×{n_mod})."
                )

            lines.append(
                f"[PRICING] multiplier={eff} | {price_note} | "
                f"Multiply base_price_cats from WORLD_CONTEXT item blocks by {eff}."
            )

            # Shop inventory
            if _in_shop:
                lines.append("shop_status: in_shop")

            sell_list = list(shop_stock) if shop_stock is not None else None

            if sell_list:
                lines.append("[SHOP INVENTORY] Items available for purchase:")
                for item_name in sell_list:
                    lines.append(f"  - {item_name}")
                lines.append(
                    "SELL RULE: ONLY sell items listed above. "
                    "If asked for something not listed, say you don't carry it."
                )
            elif sell_list == []:
                lines.append(
                    "[SHOP INVENTORY] No listed wares are available right now. "
                    "Do not promise or sell a specific item."
                )
            else:
                lines.append(
                    "[SHOP INVENTORY] Exact live shop stock is unavailable in this prompt. "
                    "Speak generally about your wares if needed, but do not promise or sell a specific item unless it is explicitly listed here."
                )

        payload = {
            "target_npc": _to_folder_name(npc_name),
            "stats_content": "\n".join(lines),
        }
        pid = str(live_ctx.get("persistent_id") or "").strip()
        rid = str(live_ctx.get("runtime_id") or live_ctx.get("id") or "").strip()
        if pid:
            payload["persistent_id"] = pid
        elif rid:
            payload["runtime_id"] = rid

        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        self.hub.write_stats(payload)

    # ─── PRICING HELPERS ───────────────────────────────────────────────

    def _get_global_price_modifier(self) -> float:
        """Read global price_modifier from Kayak config."""
        try:
            value = self.hub.get_config_value("price_modifier")
            return float(value if value is not None else 1.0)
        except (ValueError, TypeError):
            return 1.0

    def _get_city_price_modifier(self, town_name: str, campaign: Optional[str] = None) -> float:
        """Read city price_modifier from location entity."""
        if not town_name:
            return 1.0
        fields = self.get_npc_fields(town_name, None, campaign)
        try:
            raw = fields.get("price_modifier", "")
            return float(raw) if raw else 1.0
        except (ValueError, TypeError):
            return 1.0

    # ─── READ OPERATIONS ───────────────────────────────────────────────

    def get_npc_fields(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> dict:
        """Fetch NPC's entity fields from Kayak."""
        payload = {}
        if target_npc:
            payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        return self.hub.read_npc_fields(payload)

    def get_all_npc_fields(self, campaign: Optional[str] = None) -> Dict[str, Dict[str, str]]:
        """Return all indexed NPC fields for registry sync."""
        active_campaign = campaign or self.campaign
        fields = self.hub.get_all_npc_fields(active_campaign)
        if fields:
            return fields
        return self._get_all_npc_fields_from_fs(active_campaign)

    def _build_knowledge_filter(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> Optional[List[str]]:
        """Build knowledge_filter from NPC's $knows_about field."""
        fields = dict(fields or self.get_npc_fields(target_npc, target_npc_id, campaign))
        raw = fields.get("knows_about", "").strip()
        faction = str(fields.get("faction") or "").strip()
        origin_faction = str(fields.get("origin_faction") or "").strip()
        race = str(fields.get("race") or "").strip().lower()

        merged = set(_build_initial_knowledge(faction, origin_faction))
        merged |= _race_seed(race)

        # If entity has explicit location-like fields, always include them.
        for lk in ("location", "town", "town_name", "home_region", "region"):
            lv = str(fields.get(lk) or "").strip()
            if lv:
                merged.add(_to_folder_name(lv))

        if raw:
            merged |= {x.strip() for x in raw.split(",") if x.strip()}

        if not merged:
            return None
        return sorted(merged)

    def _compose_knower_context(
        self,
        stored_fields: Optional[Dict[str, Any]] = None,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(stored_fields or {})

        def _merge(dst: Dict[str, Any], key: str, value: Any):
            if value in (None, ""):
                return
            if isinstance(value, dict):
                existing = dst.get(key)
                nested = dict(existing) if isinstance(existing, dict) else {}
                for subkey, subvalue in value.items():
                    _merge(nested, str(subkey), subvalue)
                dst[key] = nested
                return
            dst[key] = value

        if isinstance(runtime_context, dict):
            for key, value in runtime_context.items():
                _merge(merged, str(key), value)

        return merged

    # ─── ACTION HINTS ──────────────────────────────────────────────────

    _ATTACK_SUPPRESS = frozenset({
        "charge", "attack", "chaaarge", "ataack", "ataaack", "ataaaaack",
        "on me", "on my mark", "to arms", "forward", "advance",
        "get them", "get him", "get her", "fight them", "kill them",
        "engage", "move out", "hit them", "rush", "destroy them",
    })

    _ATTACK_PERMIT = frozenset({
        "attack me", "fight me", "hit me", "strike me", "come at me",
        "train me", "training", "let's train", "spar", "duel me",
        "teach me to fight",
    })

    _ACTION_HINTS = [
        (
            frozenset({
                "join us", "join my squad", "join me", "join our group",
                "come with me", "come with us", "recruit you",
            }),
            "[ACTION HINT] If agreeing to join, use [ACTION: JOIN_PARTY].",
        ),
        (
            frozenset({
                "follow me", "follow us", "come here", "stay with me",
            }),
            "[ACTION HINT] Use [ACTION: FOLLOW_PLAYER] to follow.",
        ),
        (
            frozenset({
                "stay here", "wait here", "dismissed", "you're dismissed",
            }),
            "[ACTION HINT] Use [ACTION: IDLE] or [ACTION: LEAVE].",
        ),
        (
            frozenset({"heal", "medic", "injured", "bleeding"}),
            "[ACTION HINT] Use [ACTION: JOB_MEDIC] if you can help.",
        ),
        (
            frozenset({"rescue", "save them", "they're down"}),
            "[ACTION HINT] Use [ACTION: FIND_AND_RESCUE].",
        ),
        (
            frozenset({"patrol", "keep watch", "guard"}),
            "[ACTION HINT] Use [ACTION: PATROL_TOWN].",
        ),
        (
            frozenset({"go home", "return home", "back to base"}),
            "[ACTION HINT] Use [ACTION: GO_HOMEBUILDING].",
        ),
        (
            frozenset({"travel to", "go to", "head to", "journey to"}),
            "[ACTION HINT] Use [ACTION: TRAVEL_TO_TARGET_TOWN: Town Name].",
        ),
    ]

    def _build_action_hint(self, player_message: str) -> Optional[str]:
        """Build action hint from player message keywords."""
        msg = player_message.lower()
        hints = []

        _personal = any(kw in msg for kw in self._ATTACK_PERMIT)
        _cry = any(kw in msg for kw in self._ATTACK_SUPPRESS)

        if _personal:
            hints.append(
                "[ACTION HINT] Personal combat: [ACTION: ATTACK] is only valid "
                "for direct player-vs-NPC fights, not battles with enemies."
            )
        elif _cry:
            hints.append(
                "[ACTION HINT] Battle cry: do NOT use [ACTION: ATTACK] as a rally. "
                "That makes you attack the PLAYER, not the enemy."
            )

        for trigger_set, hint_text in self._ACTION_HINTS:
            if any(kw in msg for kw in trigger_set):
                hints.append(hint_text)

        return "\n".join(hints) if hints else None

    # ─── PROMPT BUILDING ───────────────────────────────────────────────

    def build_chat_prompt(
        self,
        player_message: str,
        target_npc: Optional[str] = None,
        target_npc_id: Optional[str] = None,
        race: Optional[str] = None,
        persona_category: Optional[str] = None,
        mode: str = "talk",
        extra_context: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        knowledge_filter: Optional[List[str]] = None,
        knower_context: Optional[Dict[str, Any]] = None,
        knowledge_filters_enabled: bool = True,
        campaign: Optional[str] = None,
        runtime_blocks: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build a chat prompt."""
        payload = {
            "player_message": player_message,
            "mode": mode.lower(),
        }
        if target_npc:
            payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if race:
            payload["race"] = str(race)
        if persona_category:
            payload["persona_category"] = str(persona_category)

        action_hint = self._build_action_hint(player_message)
        if action_hint:
            extra_context = ((extra_context + "\n\n") if extra_context else "") + action_hint

        if extra_context:
            payload["extra_context"] = extra_context
        if keywords:
            payload["keywords"] = keywords
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        stored_fields: Dict[str, Any] = {}
        if target_npc:
            stored_fields = self.get_npc_fields(target_npc, target_npc_id, campaign)
            payload["knower_context"] = self._compose_knower_context(
                stored_fields=stored_fields,
                runtime_context=knower_context,
            )

        npc_kf: List[str] = []
        if target_npc and knowledge_filters_enabled:
            built = self._build_knowledge_filter(
                target_npc,
                target_npc_id,
                campaign,
                fields=stored_fields,
            )
            if built:
                npc_kf = built

        if not knowledge_filters_enabled:
            payload["disable_knowledge_rules"] = True
        elif knowledge_filter:
            merged = []
            seen = set()
            for item in list(knowledge_filter) + npc_kf:
                clean = str(item).strip()
                key = clean.replace(" ", "_").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(clean)
            if merged:
                payload["knowledge_filter"] = merged
        elif npc_kf:
            payload["knowledge_filter"] = npc_kf

        if runtime_blocks:
            for _k, _v in runtime_blocks.items():
                if _v not in (None, ""):
                    payload[str(_k)] = _v

        return self.hub.build_chat_prompt(payload)

    def build_loremaster_prompt(self, events: str, campaign: Optional[str] = None) -> str:
        """Build a loremaster prompt (world narrative)."""
        return self.hub.build_loremaster_prompt(events, campaign or self.campaign)

    def build_radiant_prompt(
        self,
        speakers: List[Dict[str, Any]],
        player_name: str,
        world_lore: str,
        events: str,
        recent_dialogue: str = "",
        campaign: Optional[str] = None,
        world_synthesis_path: Optional[str] = None,
        server_town: Optional[str] = None,
        server_region: Optional[str] = None,
    ) -> str:
        """Build a category-aware radiant/group prompt."""
        payload = {
            "speakers": speakers or [],
            "player_name": player_name,
            "world_lore": world_lore,
            "events": events,
            "recent_dialogue": recent_dialogue,
        }
        if world_synthesis_path:
            payload["world_synthesis_path"] = str(world_synthesis_path)
        if server_town:
            payload["server_town"] = str(server_town)
        if server_region:
            payload["server_region"] = str(server_region)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        return self.hub.build_radiant_prompt(payload)

    def build_biography_prompt(
        self,
        npc_data: str,
        campaign: Optional[str] = None,
        race: Optional[str] = None,
        persona_category: Optional[str] = None,
    ) -> str:
        """Build a biography prompt (NPC generation)."""
        return self.hub.build_biography_prompt(
            npc_data=npc_data,
            campaign=campaign or self.campaign,
            race=str(race) if race else None,
            persona_category=str(persona_category) if persona_category else None,
        )

    # ─── WORLD EVENTS ──────────────────────────────────────────────────

    def write_world_event(
        self,
        name: str,
        summary: str,
        campaign: Optional[str] = None,
    ) -> bool:
        """Write a world event (rumor, global narrative) to Kayak."""
        try:
            return self.hub.write_world_event(
                event_name=name,
                event_content=summary,
                campaign=campaign or self.campaign,
            )
        except Exception as e:
            log.error(f"write_world_event failed: {e}")
            return False

    # ─── DIALOGUE ──────────────────────────────────────────────────────

    def save_dialogue(
        self,
        target_npc: str,
        player_line: str,
        npc_line: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
        location: Optional[str] = None,
    ):
        """Persist dialogue exchange."""
        payload = {
            "target_npc": _to_folder_name(target_npc),
            "player_line": player_line,
            "npc_line": npc_line,
        }
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        self.hub.write_dialogue(payload)

        if location:
            self.grow_npc_knowledge(target_npc, location, target_npc_id, campaign)

    def read_dialogue(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
        keep_lines: int = 30,
    ) -> list:
        """Read NPC's dialogue history."""
        payload = {"keep_lines": keep_lines}
        if target_npc:
            payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        lines = self.hub.read_dialogue(payload)
        if lines:
            return lines
        # Compatibility fallback for Kayak builds where /dialogue/read is absent/broken.
        return self._read_dialogue_from_fs(target_npc, target_npc_id, campaign or self.campaign, keep_lines)

    def cull_future_dialogue(
        self,
        day: int,
        hour: int,
        minute: int,
        campaign: Optional[str] = None,
        dry_run: bool = False,
        reload_after: bool = True,
    ) -> dict:
        """Cull all NPC dialogue lines after the supplied in-game timestamp."""
        payload = {
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
            "dry_run": bool(dry_run),
            "reload_after": bool(reload_after),
        }
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        try:
            return self.hub.cull_future_dialogue(payload)
        except Exception as e:
            log.error(f"cull_future_dialogue failed: {e}")
            return {"status": "error", "error": str(e)}

    def clear_dialogue(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> bool:
        """Clear dialogue.txt contents for one NPC while keeping the file."""
        payload = {"lines": [], "keep_lines": 0}
        if target_npc:
            payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        ok = self.hub.replace_dialogue(payload)
        if not ok:
            return False

        field_payload = {"field": "has_dialogue", "value": "0"}
        if target_npc:
            field_payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            field_payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            field_payload["campaign"] = campaign or self.campaign
        try:
            self.hub.write_entity_field(field_payload)
        except Exception:
            pass
        return True

    def grow_npc_knowledge(
        self,
        target_npc: str,
        location: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ):
        """Add location to NPC's $knows_about field."""
        loc = _to_folder_name(location)
        if not loc:
            return
        payload = {"target_npc": _to_folder_name(target_npc), "location": loc}
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        self.hub.write_npc_knowledge(
            payload["target_npc"],
            loc,
            payload.get("campaign"),
            payload.get("target_npc_id"),
        )

    def save_overheard_dialogue(
        self,
        target_npc: str,
        player_line: str,
        npc_line: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
        location: Optional[str] = None,
    ):
        """Persist an overheard dialogue for a bystander."""
        payload = {
            "target_npc": _to_folder_name(target_npc),
            "player_line": f"(Overheard) {player_line}" if player_line else "",
            "npc_line": f"(Overheard) {npc_line}" if npc_line else "",
        }
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign

        self.hub.write_dialogue(payload)

        if location:
            self.grow_npc_knowledge(target_npc, location, target_npc_id, campaign)

    def build_speak_prompt(
        self,
        line: str,
        target_npc: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> str:
        """Build speak prompt (echo mode)."""
        return self.hub.build_speak_prompt(line, target_npc, campaign or self.campaign)

    # ─── CHARACTER CREATION ─────────────────────────────────────────────

    def create_npc_entity(
        self,
        char_data: Dict[str, Any],
        persistent_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> Optional[str]:
        """
        Create/update an NPC entity in Kayak from SentientSands character data.
        
        Args:
            char_data: Complete character profile (Name, Personality, Backstory, etc.)
            persistent_id: UUID (generated if not provided)
            campaign: Campaign name (uses self.campaign if not provided)
        
        Returns:
            persistent_id if successful, None on error
        """
        campaign = campaign or self.campaign
        if not campaign:
            log.error("create_npc_entity: no campaign specified")
            return None
        
        npc_name = str(char_data.get("Name", "Unknown")).strip()
        if not npc_name:
            log.error("create_npc_entity: Name field is required")
            return None
        
        # Generate ID if not provided
        if not persistent_id:
            existing_id = str(
                char_data.get("ID")
                or char_data.get("persistent_id")
                or ""
            ).strip()
            persistent_id = existing_id or str(uuid.uuid4())

        profile = dict(char_data)
        profile["Name"] = npc_name
        profile["ID"] = persistent_id

        success = self.write_npc_profile(profile, campaign)
        if not success:
            log.error(f"create_npc_entity: write_npc_profile failed for {npc_name}")
            return None
        
        log.info(f"Created NPC entity: {npc_name} (id={persistent_id})")
        return persistent_id

    def create_batch_npc_entities(
        self,
        char_data_list: List[Dict[str, Any]],
        campaign: Optional[str] = None,
    ) -> List[tuple]:
        """
        Create multiple NPCs efficiently (single batch operation).
        
        Args:
            char_data_list: List of character profiles
            campaign: Campaign name
        
        Returns:
            List of (npc_name, persistent_id) tuples for successful creations
        """
        campaign = campaign or self.campaign
        if not campaign:
            log.error("create_batch_npc_entities: no campaign specified")
            return []

        results = []
        
        # Process in parallel threads to avoid slow sequential HTTP calls
        # But limit concurrency to avoid Kayak overload
        MAX_WORKERS = 4
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def _create_one(char_data):
            try:
                npc_name = str(char_data.get("Name", "")).strip()
                if not npc_name:
                    return None
                persistent_id = str(
                    char_data.get("ID")
                    or char_data.get("persistent_id")
                    or uuid.uuid4()
                )
                profile = dict(char_data)
                profile["ID"] = persistent_id
                success = self.write_npc_profile(profile, campaign)
                if success:
                    return (npc_name, persistent_id)
                return None
            except Exception as e:
                log.warning(f"create_batch_npc_entities: failed for {char_data.get('Name')}: {e}")
                return None
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(_create_one, cd) for cd in char_data_list]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        
        log.info(f"Batch created {len(results)}/{len(char_data_list)} NPCs")
        return results

    def on_rename(
        self,
        old_name: str,
        new_name: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> bool:
        """
        Post-write rename follow-up for in-game /name changes.

        The main server rename handler already persisted the updated profile
        before calling this hook. We only need to sync the Kayak entity folder
        plus the canonical Name/display_name fields with the new in-game name.
        """
        _new = _clean_name(new_name)
        if not _new:
            return False

        old_folder = _to_folder_name(old_name)
        new_folder = _to_folder_name(_new)
        if not old_folder or not new_folder or old_folder == new_folder:
            return True

        payload = {
            "old_name": old_folder,
            "new_name": new_folder,
            "new_display_name": _new,
            "campaign": campaign or self.campaign,
        }
        if target_npc_id:
            stable_id = str(target_npc_id).strip()
            if stable_id:
                payload["target_npc_id"] = stable_id
                payload["id"] = stable_id

        ok = self.hub.rename_npc(payload)
        if ok:
            log.info(f"on_rename: synced Kayak entity {old_folder} -> {new_folder}")
        else:
            log.warning(f"on_rename: failed to sync Kayak entity {old_folder} -> {new_folder}")
        return ok

    # ─── STATUS ────────────────────────────────────────────────────────

    def is_alive(self, force: bool = False, retry_secs: float = 5.0) -> bool:
        """Check if Kayak server is reachable."""
        return self.hub.is_alive(retry_secs=retry_secs, force=force)

    # ─── COMPATIBILITY ADAPTER ─────────────────────────────────────────

    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compatibility adapter for legacy SS callers that still expect a Hub-like
        execute({op, campaign, data}) contract.
        """
        op = str(payload.get("op") or "").strip().upper()
        campaign = payload.get("campaign") or self.campaign
        data = payload.get("data") or {}

        try:
            if op == Ops.LIST_CHARACTERS:
                npcs = self.get_all_npc_fields(campaign)
                names = sorted(npcs.keys())
                return {"ok": True, "data": names, "errors": [], "warnings": []}

            if op == Ops.READ_PROFILE:
                target_name = data.get("name") or data.get("target_npc") or ""
                target_id = (
                    data.get("persistent_id")
                    or data.get("target_npc_id")
                    or data.get("runtime_id")
                )
                fields = self.get_npc_fields(target_name, target_id, campaign)
                if not fields:
                    return {"ok": False, "data": None, "errors": ["Profile not found"], "warnings": []}

                keep_lines = int(data.get("keep_dialogue_lines") or data.get("keep_lines") or 45)
                history = self.read_dialogue(target_name, target_id, campaign, keep_lines)
                profile_id = (
                    target_id
                    or fields.get("persistent_id")
                    or fields.get("runtime_id")
                    or fields.get("id")
                    or _to_folder_name(target_name)
                )
                result = {
                    "ID": profile_id,
                    "Name": (fields.get("display_name") or target_name or profile_id).strip(),
                    "Race": (fields.get("race") or "Unknown").strip(),
                    "Sex": (fields.get("sex") or "Unknown").strip(),
                    "Faction": (fields.get("faction") or "Unknown").strip(),
                    "OriginFaction": (fields.get("origin_faction") or fields.get("faction") or "Unknown").strip(),
                    "Job": (fields.get("role") or fields.get("job") or "None").strip(),
                    "Personality": (fields.get("personality") or "").strip(),
                    "Backstory": (fields.get("backstory") or "").strip(),
                    "SpeechQuirks": (fields.get("speech_quirks") or "").strip(),
                    "Traits": {},
                    "Relation": _safe_int(fields.get("relation"), 0),
                    "ConversationHistory": list(history),
                }
                for t in ("loyalty", "religion", "outlook", "motivation"):
                    val = (fields.get(t) or "").strip()
                    if val:
                        result["Traits"][t.capitalize()] = val
                return {"ok": True, "data": result, "errors": [], "warnings": []}

            if op == Ops.WRITE_PROFILE:
                profile = dict(data)
                if payload.get("campaign") and "campaign" not in profile:
                    profile["campaign"] = payload["campaign"]
                ok = self.write_npc_profile(profile, campaign)
                if ok and "ConversationHistory" in profile:
                    target_id = str(
                        profile.get("ID")
                        or profile.get("persistent_id")
                        or ""
                    ).strip()
                    _wr_keep = int(data.get("keep_dialogue_lines") or data.get("keep_lines") or 45)
                    replace_payload = {
                        "lines": list(profile.get("ConversationHistory") or []),
                        "keep_lines": _wr_keep,
                    }
                    if profile.get("Name"):
                        replace_payload["target_npc"] = _to_folder_name(profile["Name"])
                    if target_id:
                        replace_payload["target_npc_id"] = target_id
                    if campaign:
                        replace_payload["campaign"] = campaign
                    # Idempotency guard: only sync when latest exchange changed.
                    hist_lines = list(profile.get("ConversationHistory") or [])
                    sig = self._dialogue_signature(hist_lines)
                    sid_for_sig = str(target_id or profile.get("Name") or "").strip()
                    campaign_key = str(campaign or self.campaign or "").strip()
                    sig_key = f"{campaign_key}::{sid_for_sig}" if sid_for_sig else ""
                    if sig_key and sig:
                        prev = self._last_dialogue_sync_sig.get(sig_key)
                        # Recovery guard: if cache says "already synced" but file
                        # is absent (new campaign / manual cleanup), force rewrite.
                        needs_seed = False
                        if prev == sig:
                            fs_lines = self._read_dialogue_from_fs(
                                target_npc=str(profile.get("Name") or ""),
                                target_npc_id=target_id or None,
                                campaign=campaign or self.campaign,
                                keep_lines=1,
                            )
                            needs_seed = len(fs_lines) == 0
                        if prev != sig or needs_seed:
                            self.hub.replace_dialogue(replace_payload)
                            self._last_dialogue_sync_sig[sig_key] = sig
                    else:
                        self.hub.replace_dialogue(replace_payload)
                return {
                    "ok": bool(ok),
                    "data": {"name": profile.get("Name")},
                    "errors": [] if ok else ["Write failed"],
                    "warnings": [],
                }

            return {"ok": False, "data": None, "errors": [f"Unknown operation: {op}"], "warnings": []}
        except Exception as e:
            log.error(f"execute({op}) failed: {e}", exc_info=True)
            return {"ok": False, "data": None, "errors": [str(e)], "warnings": []}

    # ─── IN-GAME KAYAK COMMANDS ───────────────────────────────────────

    def _sentient_songs(self):
        """Return the SentientSongs controller client."""
        return self.sentient_songs

    def k_help(self) -> str:
        return (
            "[KAYAK] Commands: /k_visited <place>, /k_job <text>, /k_backstory <text>, "
            "/k_speech <text>, /k_note <field> <value>, /k_npc_prices <x>, "
            "/k_global_prices <x>, /k_city_prices <city> <x>, /k_speak <line>, "
            "/k_clean. "
            "[SentientSongs] /m_help, /m_play <song>, /m_folder <folder>, "
            "/m_playlist <list>, /m_find <keyword>, /m_pause, /m_resume, "
            "/m_stop, /m_next, /m_prev, /m_vol <0-100>, /m_shuffle on|off, "
            "/m_loop off|track|folder, /m_reload."
        )

    def k_mhelp(self) -> str:
        if not self._sentient_songs():
            return "[MUSIC] SentientSongs client is unavailable."
        return self._sentient_songs().send_command("/m_help")

    def k_mdispatch(self, command_text: str) -> str:
        if not self._sentient_songs():
            return "[MUSIC] SentientSongs client is unavailable."
        if isinstance(command_text, str) and command_text.startswith("/k_m"):
            command_text = "/m_" + command_text[4:]
        return self._sentient_songs().send_command(command_text)

    def k_mplay(self, song_name: str) -> str:
        return self.k_mdispatch(f"/m_play {song_name}")

    def k_mfolder(self, folder_name: str) -> str:
        return self.k_mdispatch(f"/m_folder {folder_name}")

    def k_mplaylist(self, playlist_name: str) -> str:
        return self.k_mdispatch(f"/m_playlist {playlist_name}")

    def k_mfind(self, keyword: str) -> str:
        return self.k_mdispatch(f"/m_find {keyword}")

    def k_mpause(self) -> str:
        return self.k_mdispatch("/m_pause")

    def k_mresume(self) -> str:
        return self.k_mdispatch("/m_resume")

    def k_mstop(self) -> str:
        return self.k_mdispatch("/m_stop")

    def k_mnext(self) -> str:
        return self.k_mdispatch("/m_next")

    def k_mprev(self) -> str:
        return self.k_mdispatch("/m_prev")

    def k_mvol(self, volume: int) -> str:
        return self.k_mdispatch(f"/m_vol {volume}")

    def k_mshuffle(self, state: str) -> str:
        return self.k_mdispatch(f"/m_shuffle {state}")

    def k_mloop(self, mode: str) -> str:
        return self.k_mdispatch(f"/m_loop {mode}")

    def k_mreload(self) -> str:
        return self.k_mdispatch("/m_reload")

    def k_mstatus(self) -> str:
        return self.k_mdispatch("/m_status")

    def k_visited(self, target_npc: str, location: str, target_npc_id: Optional[str] = None,
                  campaign: Optional[str] = None) -> str:
        self.grow_npc_knowledge(target_npc, location, target_npc_id, campaign)
        return f"[KAYAK] {target_npc} now knows about {location}."

    def k_job(self, target_npc: str, value: str, target_npc_id: Optional[str] = None,
              campaign: Optional[str] = None) -> str:
        return self._set_npc_field(target_npc, "role", value, target_npc_id, campaign, "job")

    def k_backstory(self, target_npc: str, value: str, target_npc_id: Optional[str] = None,
                    campaign: Optional[str] = None) -> str:
        return self._set_npc_field(target_npc, "$backstory", value, target_npc_id, campaign, "backstory")

    def k_speech(self, target_npc: str, value: str, target_npc_id: Optional[str] = None,
                 campaign: Optional[str] = None) -> str:
        return self._set_npc_field(target_npc, "$speech_quirks", value, target_npc_id, campaign, "speech")

    def k_note(self, target_npc: str, field: str, value: str, target_npc_id: Optional[str] = None,
               campaign: Optional[str] = None) -> str:
        clean_field = field.strip()
        if not clean_field:
            return "[KAYAK] Field name is required."
        if clean_field.startswith("$") or clean_field in {"weight", "relation", "faction", "origin_faction", "role"}:
            kayak_field = clean_field
        else:
            kayak_field = f"${clean_field}"
        return self._set_npc_field(target_npc, kayak_field, value, target_npc_id, campaign, clean_field)

    def k_npc_prices(self, target_npc: str, multiplier: float, target_npc_id: Optional[str] = None,
                     campaign: Optional[str] = None) -> str:
        return self._set_npc_field(
            target_npc,
            "price_modifier",
            str(multiplier),
            target_npc_id,
            campaign,
            "price modifier",
        )

    def set_global_price_modifier(self, multiplier: float) -> bool:
        return self.hub.set_config_value("price_modifier", float(multiplier))

    def set_city_price_modifier(self, city: str, multiplier: float, campaign: Optional[str] = None) -> bool:
        payload = {
            "name": _to_folder_name(city),
            "field": "price_modifier",
            "value": str(float(multiplier)),
        }
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        return self.hub.write_entity_field(payload)

    def _set_npc_field(
        self,
        target_npc: str,
        field: str,
        value: str,
        target_npc_id: Optional[str],
        campaign: Optional[str],
        label: str,
    ) -> str:
        payload = {
            "name": _to_folder_name(target_npc),
            "field": field,
            "value": value,
        }
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        ok = self.hub.write_entity_field(payload)
        if ok:
            return f"[KAYAK] {target_npc} {label} updated."
        return f"[KAYAK] Failed to update {label} for {target_npc}."

    def clear_npc_generated_profile_fields(
        self,
        target_npc: str,
        target_npc_id: Optional[str] = None,
        campaign: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Blank LLM-generated prose fields without removing the field lines."""
        payload: Dict[str, Any] = {
            "fields": list(fields or ["$personality", "$backstory", "$speech_quirks", "$speechquirks", "$biography"]),
        }
        if target_npc:
            payload["target_npc"] = _to_folder_name(target_npc)
        if target_npc_id:
            payload["target_npc_id"] = str(target_npc_id)
        if campaign or self.campaign:
            payload["campaign"] = campaign or self.campaign
        return self.hub.clear_entity_fields(payload)

    def _get_all_npc_fields_from_fs(self, campaign: Optional[str]) -> Dict[str, Dict[str, str]]:
        """
        Fallback read path for older Kayak server builds missing /entity/all_npcs.
        Reads runtime NPC entity.txt files directly from KayakDB.
        """
        if not campaign:
            return {}

        kayak_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        categories_root = os.path.join(
            kayak_root,
            "KayakDB",
            "Campaigns",
            campaign,
            "categories",
        )
        if not os.path.isdir(categories_root):
            return {}

        result: Dict[str, Dict[str, str]] = {}
        for category in ("campaign_npcs", "base_npcs"):
            npcs_root = os.path.join(categories_root, category)
            if not os.path.isdir(npcs_root):
                continue

            for folder in os.listdir(npcs_root):
                entity_path = os.path.join(npcs_root, folder, "entity.txt")
                if not os.path.isfile(entity_path):
                    continue

                data = _parse_entity_file(entity_path)
                data.setdefault("category", category)
                data.setdefault("_entity_category", category)
                data.setdefault("_entity_name", folder)
                try:
                    data["mtime"] = os.path.getmtime(entity_path)
                except OSError:
                    data["mtime"] = 0.0

                display_name = (
                    data.get("display_name")
                    or data.get("name")
                    or folder
                )
                key = str(display_name)
                if key in result:
                    suffix = (
                        data.get("persistent_id")
                        or data.get("runtime_id")
                        or data.get("id")
                        or folder
                    )
                    key = f"{key}__{suffix}"
                result[key] = data
        return result

    def _read_dialogue_from_fs(
        self,
        target_npc: str,
        target_npc_id: Optional[str],
        campaign: Optional[str],
        keep_lines: int,
    ) -> List[str]:
        if not campaign:
            return []

        kayak_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        categories_root = os.path.join(
            kayak_root,
            "KayakDB",
            "Campaigns",
            campaign,
            "categories",
        )
        if not os.path.isdir(categories_root):
            return []

        search_roots = [
            os.path.join(categories_root, category)
            for category in ("campaign_npcs", "base_npcs")
            if os.path.isdir(os.path.join(categories_root, category))
        ]
        if not search_roots:
            return []

        target_folder = _to_folder_name(target_npc) if target_npc else ""
        dialogue_path = ""

        if target_folder:
            for npcs_root in search_roots:
                candidate = os.path.join(npcs_root, target_folder, "dialogue.txt")
                if os.path.isfile(candidate):
                    dialogue_path = candidate
                    break

        # ID-based fallback: locate entity folder by persistent/runtime/header id.
        if not dialogue_path and target_npc_id:
            for npcs_root in search_roots:
                for folder in os.listdir(npcs_root):
                    entity_path = os.path.join(npcs_root, folder, "entity.txt")
                    if not os.path.isfile(entity_path):
                        continue
                    fields = _parse_entity_file(entity_path)
                    if target_npc_id in {
                        fields.get("id", ""),
                        fields.get("persistent_id", ""),
                        fields.get("runtime_id", ""),
                    }:
                        candidate = os.path.join(npcs_root, folder, "dialogue.txt")
                        if os.path.isfile(candidate):
                            dialogue_path = candidate
                        break
                if dialogue_path:
                    break

        if not dialogue_path:
            return []

        try:
            with open(dialogue_path, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            if keep_lines and len(lines) > keep_lines:
                lines = lines[-keep_lines:]
            return lines
        except OSError:
            return []

    @staticmethod
    def _dialogue_signature(lines: List[str]) -> str:
        cleaned = [str(x).strip() for x in lines if str(x).strip()]
        if not cleaned:
            return ""
        tail = cleaned[-2:] if len(cleaned) >= 2 else cleaned[-1:]
        return "||".join(tail)


# ─── UTILITIES ─────────────────────────────────────────────────────────────────

def _clean_name(name: str) -> str:
    """Strip pipe-separated IDs."""
    return str(name).split("|")[0].strip() if name else ""


def _to_folder_name(name: str) -> str:
    """Convert to filesystem-safe folder name."""
    clean = _clean_name(name)
    return re.sub(r"[^\w\-]", "_", clean).strip("_")


def _read_text(path: str) -> str:
    """Read text file safely."""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                return f.read()
    except Exception as e:
        log.debug(f"_read_text({path}): {e}")
    return ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_entity_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                k = key.strip().lstrip("$").lower().replace(" ", "_")
                v = value.strip()
                out[k] = v
    except OSError:
        return {}

    # Preserve common id aliases expected by callers
    if "id" not in out:
        out["id"] = out.get("persistent_id") or out.get("runtime_id") or ""
    return out
