"""
ss_character_gateway.py — Kayak-only character persistence gateway.

THE canonical read/write interface for character data in SentientSands.
All character I/O goes through this gateway. No filesystem, no legacy paths.

Design principles:
  - One read path, one write path, one cache.
  - Reads never trigger writes unless data was actually modified.
  - Writes are merge-mode: only touched fields change in Kayak.
  - Manual edits to entity.txt survive because unrecognized fields are preserved.
  - Every write logs its reason for debugging.

The gateway talks to the SentientSandsBridge (which talks to KayakHub,
which talks to Kayak server). The server only talks to the gateway.

[Design: Pineaxe]
"""

import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("ss.gateway")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — CANONICAL FIELD MAP
# ═══════════════════════════════════════════════════════════════════════════════
#
# This is the single source of truth for how SS dict keys map to Kayak entity
# field names. Both read() and write() use this map. If a field needs adding
# or renaming, change it HERE and nowhere else.
#
# SS dict key          Kayak entity field      Notes
# ──────────────────   ─────────────────────   ────────────────────────────────
# Name                 display_name            DLL reads: plain string
# ID                   persistent_id           DLL reads: identity binding
# Race                 race                    DLL reads: plain string
# Sex                  sex                     DLL reads: plain string
# Faction              faction                 DLL reads: routing & display
# OriginFaction        origin_faction          Prompt-only
# Job                  role                    Prompt-only
# Relation             relation                int, affects weight
# Personality          $personality            Content: Kayak-marker-safe
# Backstory            $backstory              Content: Kayak-marker-safe
# SpeechQuirks         $speech_quirks          Content: Kayak-marker-safe
# Traits               loyalty/religion/       Assembled from individual fields
#                      outlook/motivation
# ConversationHistory  dialogue.txt            Separate file in KayakDB
# ═══════════════════════════════════════════════════════════════════════════════

# Structural fields: DLL reads these. Must be clean, JSON-safe strings.
STRUCTURAL_MAP: Dict[str, str] = {
    "Name":          "display_name",
    "Race":          "race",
    "Sex":           "sex",
    "Faction":       "faction",
    "OriginFaction": "origin_faction",
    "Job":           "role",
}

# ID fields: used for identity binding, not sent to prompt.
ID_FIELD = "persistent_id"  # Kayak field name for the primary ID
ID_FALLBACKS = ("runtime_id", "id")  # Kayak field names to try if persistent_id missing

# Numeric fields: stored as int in the SS dict.
NUMERIC_MAP: Dict[str, str] = {
    "Relation": "relation",
}

# Content fields: DLL passes through untouched. Prompt builder consumes them.
# These can safely carry Kayak marker notation ((()), $prose, @tags, & lists, >>priority<<).
CONTENT_MAP: Dict[str, str] = {
    "Personality":  "personality",
    "Backstory":    "backstory",
    "SpeechQuirks": "speech_quirks",
}

# Internal metadata: server-only profile lifecycle flags kept in entity.txt.
INTERNAL_FIELD_MAP: Dict[str, str] = {
    "_profile_state": "profile_state",
    "_has_dialogue":  "has_dialogue",
    "_persona_category": "persona_category",
}

# Trait fields: individual Kayak fields assembled into a Traits dict.
TRAIT_FIELDS = ("loyalty", "religion", "outlook", "motivation")

# Reverse maps (Kayak field → SS key) built once at import time.
_KAYAK_TO_SS: Dict[str, str] = {}
for _ss, _kf in STRUCTURAL_MAP.items():
    _KAYAK_TO_SS[_kf] = _ss
for _ss, _kf in NUMERIC_MAP.items():
    _KAYAK_TO_SS[_kf] = _ss
for _ss, _kf in CONTENT_MAP.items():
    _KAYAK_TO_SS[_kf] = _ss
for _ss, _kf in INTERNAL_FIELD_MAP.items():
    _KAYAK_TO_SS[_kf] = _ss


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — CHARACTER GATEWAY
# ═══════════════════════════════════════════════════════════════════════════════

class CharacterGateway:
    """
    The ONE interface for character data.

    Receives a SentientSandsBridge instance at construction.
    All methods accept campaign as a parameter (it can change at runtime).
    """

    def __init__(self, bridge=None, max_cache: int = 200):
        self._bridge = bridge
        self._cache: Dict[str, dict] = {}        # storage_id → SS dict
        self._cache_lock = threading.Lock()
        self._max_cache = max_cache
        self._dirty_sigs: Dict[str, str] = {}    # storage_id → content hash
        self._dialogue_limit = 45                 # configurable via set_dialogue_limit()

    # ─── PUBLIC: CONFIGURE ──────────────────────────────────────────────

    def set_bridge(self, bridge) -> None:
        """Update the bridge reference (e.g. after Kayak reconnects)."""
        self._bridge = bridge

    def set_dialogue_limit(self, limit: int) -> None:
        """Set max dialogue lines kept per NPC. Called from init_server_state()."""
        self._dialogue_limit = max(1, int(limit))

    @property
    def available(self) -> bool:
        """True if the bridge is connected and Kayak is reachable."""
        return self._bridge is not None and self._bridge.is_alive()

    # ─── PUBLIC: READ ───────────────────────────────────────────────────

    def read(
        self,
        name: str,
        persistent_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Read a character from Kayak and return as SS dict.

        Checks in-memory cache first. On cache miss, queries Kayak via bridge.
        Returns None if the character doesn't exist in Kayak.
        """
        cache_key = self._cache_key(name, persistent_id)

        # 1. Cache hit
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                # Refresh LRU position
                self._cache.pop(cache_key, None)
                self._cache[cache_key] = cached
                return dict(cached)  # return a copy so callers can mutate

        # 2. Bridge read
        if not self._bridge:
            log.warning(f"GATEWAY-READ: bridge unavailable for {name}")
            return None

        fields = self._bridge.get_npc_fields(name, persistent_id, campaign)
        if not fields:
            log.debug(f"GATEWAY-READ: {name} not found in Kayak")
            return None

        # Resolve the actual entity folder name for stable identity
        entity_name = fields.get("_entity_name", "")

        # Read dialogue separately
        history = self._bridge.read_dialogue(
            name, persistent_id, campaign, keep_lines=self._dialogue_limit
        )

        # 3. Translate Kayak fields → SS dict
        ss_dict = self._kayak_to_ss_dict(fields, history, persistent_id)

        # 4. Cache the result
        self._cache_put(cache_key, ss_dict)
        log.debug(f"GATEWAY-READ: {name} loaded from Kayak")
        return dict(ss_dict)

    def list_characters(self, campaign: Optional[str] = None) -> list:
        """
        List all character names/IDs from Kayak index.
        Returns list of {"display": str, "sid": str} dicts.
        """
        if not self._bridge:
            return []

        try:
            all_fields = self._bridge.get_all_npc_fields(campaign)
            result = []
            for display_name, field_dict in (all_fields or {}).items():
                safe_fields = field_dict or {}
                resolved_display = str(
                    safe_fields.get("display_name") or display_name or ""
                ).strip()
                sid = str(
                    safe_fields.get("persistent_id")
                    or safe_fields.get("id")
                    or safe_fields.get("runtime_id")
                    or resolved_display
                ).strip()
                if sid and resolved_display:
                    result.append({"display": resolved_display, "sid": sid})
            return result
        except Exception as e:
            log.error(f"GATEWAY-LIST: failed: {e}")
            return []

    # ─── PUBLIC: WRITE ──────────────────────────────────────────────────

    def write(
        self,
        ss_dict: dict,
        campaign: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Write character data to Kayak.

        Performs a dirty check: if the content hasn't changed since the last
        write, the save is skipped (unless reason is 'force' or 'generation').

        The bridge's write_npc_profile() uses merge-mode: only mapped fields
        are updated in entity.txt, custom/manual fields are preserved.

        Returns True if data was written, False if skipped or failed.
        """
        if not ss_dict:
            return False
        if not self._bridge:
            log.warning(f"GATEWAY-WRITE: bridge unavailable, reason={reason}")
            return False

        name = ss_dict.get("Name", "")
        storage_id = ss_dict.get("ID", "")
        cache_key = self._cache_key(name, storage_id)

        # Dirty check: skip if content hasn't changed
        always_write = reason in ("force", "generation", "regeneration", "rename")
        if not always_write:
            new_sig = self._content_signature(ss_dict)
            old_sig = self._dirty_sigs.get(cache_key)
            if old_sig and old_sig == new_sig:
                log.debug(f"GATEWAY-WRITE: skipped (clean) for {name}, reason={reason}")
                return True  # not an error — data is already current

        # Truncate dialogue before write
        if "ConversationHistory" in ss_dict and len(ss_dict["ConversationHistory"]) > self._dialogue_limit:
            ss_dict["ConversationHistory"] = ss_dict["ConversationHistory"][-self._dialogue_limit:]

        if "ConversationHistory" in ss_dict:
            ss_dict["_has_dialogue"] = "1" if ss_dict["ConversationHistory"] else str(ss_dict.get("_has_dialogue") or "0")

        # Write profile via bridge (merge-mode)
        ok = self._bridge.write_npc_profile(ss_dict, campaign)
        if not ok:
            log.error(f"GATEWAY-WRITE: bridge write failed for {name}, reason={reason}")
            return False

        # Write dialogue if present
        history = ss_dict.get("ConversationHistory", [])
        if history:
            self._write_dialogue(ss_dict, campaign)

        # Update cache and dirty signature
        new_sig = self._content_signature(ss_dict)
        self._cache_put(cache_key, ss_dict)
        self._dirty_sigs[cache_key] = new_sig

        # Keep the plain-name cache alias in sync too. Some UI/history paths still
        # resolve NPCs by display name only, and otherwise they can keep serving a
        # stale placeholder even after a regeneration wrote the real profile under a
        # more specific storage ID.
        name_key = self._cache_key(name, None)
        if name_key and name_key != cache_key:
            self._cache_put(name_key, ss_dict)
            self._dirty_sigs[name_key] = new_sig

        log.info(f"GATEWAY-WRITE: {name} ({storage_id}), reason={reason}")
        return True

    def write_dialogue_only(
        self,
        name: str,
        persistent_id: Optional[str],
        lines: list,
        campaign: Optional[str] = None,
    ) -> bool:
        """
        Write ONLY dialogue history — no profile rewrite.
        Used by /log to persist conversation without touching entity.txt.
        """
        if not self._bridge:
            return False

        from bridges.sentient_sands.bridge import _to_folder_name
        payload = {
            "lines": list(lines),
            "keep_lines": self._dialogue_limit,
        }
        if name:
            payload["target_npc"] = _to_folder_name(name)
        if persistent_id:
            payload["target_npc_id"] = str(persistent_id)
        if campaign:
            payload["campaign"] = campaign

        try:
            ok = self._bridge.hub.replace_dialogue(payload)
            if ok:
                field_payload = {"field": "has_dialogue", "value": "1" if lines else "0"}
                if name:
                    field_payload["target_npc"] = _to_folder_name(name)
                if persistent_id:
                    field_payload["target_npc_id"] = str(persistent_id)
                if campaign:
                    field_payload["campaign"] = campaign
                try:
                    self._bridge.hub.write_entity_field(field_payload)
                except Exception as field_err:
                    log.debug(f"GATEWAY-DIALOGUE: failed to persist has_dialogue for {name}: {field_err}")

                # Update dialogue in cache too
                cache_key = self._cache_key(name, persistent_id)
                with self._cache_lock:
                    cached = self._cache.get(cache_key)
                    if cached:
                        cached["ConversationHistory"] = list(lines[-self._dialogue_limit:])
                        cached["_has_dialogue"] = "1" if lines else "0"
                        self._dirty_sigs[cache_key] = self._content_signature(cached)
                    name_key = self._cache_key(name, None)
                    if name_key and name_key != cache_key:
                        cached_name = self._cache.get(name_key)
                        if cached_name:
                            cached_name["ConversationHistory"] = list(lines[-self._dialogue_limit:])
                            cached_name["_has_dialogue"] = "1" if lines else "0"
                            self._dirty_sigs[name_key] = self._content_signature(cached_name)
            return ok
        except Exception as e:
            log.error(f"GATEWAY-DIALOGUE: failed for {name}: {e}")
            return False

    def write_entity_only(
        self,
        ss_dict: dict,
        campaign: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Write ONLY entity/profile state without touching dialogue.txt.

        Used by first-contact hydration so profile lifecycle and dialogue can be
        persisted in a deterministic order.
        """
        if not ss_dict:
            return False
        if not self._bridge:
            log.warning(f"GATEWAY-WRITE-ENTITY: bridge unavailable, reason={reason}")
            return False

        name = ss_dict.get("Name", "")
        storage_id = ss_dict.get("ID", "")
        cache_key = self._cache_key(name, storage_id)

        always_write = reason in (
            "force",
            "generation",
            "regeneration",
            "rename",
            "hydration",
            "hydration_promote",
        )
        if not always_write:
            new_sig = self._content_signature(ss_dict)
            old_sig = self._dirty_sigs.get(cache_key)
            if old_sig and old_sig == new_sig:
                log.debug(f"GATEWAY-WRITE-ENTITY: skipped (clean) for {name}, reason={reason}")
                return True

        if "ConversationHistory" in ss_dict and len(ss_dict["ConversationHistory"]) > self._dialogue_limit:
            ss_dict["ConversationHistory"] = ss_dict["ConversationHistory"][-self._dialogue_limit:]

        if "ConversationHistory" in ss_dict:
            history = list(ss_dict.get("ConversationHistory", []) or [])
            if str(ss_dict.get("_profile_state") or "").strip().lower() == "hydrating_intro":
                ss_dict["_has_dialogue"] = str(ss_dict.get("_has_dialogue") or "0")
            else:
                ss_dict["_has_dialogue"] = "1" if history else str(ss_dict.get("_has_dialogue") or "0")

        ok = self._bridge.write_npc_profile(ss_dict, campaign)
        if not ok:
            log.error(f"GATEWAY-WRITE-ENTITY: bridge write failed for {name}, reason={reason}")
            return False

        new_sig = self._content_signature(ss_dict)
        self._cache_put(cache_key, ss_dict)
        self._dirty_sigs[cache_key] = new_sig

        name_key = self._cache_key(name, None)
        if name_key and name_key != cache_key:
            self._cache_put(name_key, ss_dict)
            self._dirty_sigs[name_key] = new_sig

        log.info(f"GATEWAY-WRITE-ENTITY: {name} ({storage_id}), reason={reason}")
        return True

    # ─── PUBLIC: CACHE MANAGEMENT ───────────────────────────────────────

    def invalidate(self, name: Optional[str] = None, persistent_id: Optional[str] = None) -> None:
        """Bust a specific cache entry, or all entries if both args are None."""
        with self._cache_lock:
            if name is None and persistent_id is None:
                self._cache.clear()
                self._dirty_sigs.clear()
                log.info("GATEWAY-CACHE: invalidated all entries")
            else:
                key = self._cache_key(name or "", persistent_id)
                self._cache.pop(key, None)
                self._dirty_sigs.pop(key, None)
                log.debug(f"GATEWAY-CACHE: invalidated {key}")

    def reload(
        self,
        name: str,
        persistent_id: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> Optional[dict]:
        """Force re-read from Kayak (invalidate + read)."""
        self.invalidate(name, persistent_id)
        return self.read(name, persistent_id, campaign)

    # ─── INTERNAL: TRANSLATION ──────────────────────────────────────────

    def _kayak_to_ss_dict(
        self,
        fields: Dict[str, str],
        history: List[str],
        persistent_id: Optional[str] = None,
    ) -> dict:
        """
        Translate Kayak entity fields + dialogue lines → SS transport dict.

        This is the ONE place where this translation happens.
        """
        # Resolve ID
        resolved_id = str(
            persistent_id
            or fields.get(ID_FIELD)
            or ""
        ).strip()
        if not resolved_id:
            for fb in ID_FALLBACKS:
                resolved_id = str(fields.get(fb) or "").strip()
                if resolved_id:
                    break

        # Build the SS dict
        result: dict = {
            "ID": resolved_id or fields.get("_entity_name", ""),
        }

        # Structural fields
        for ss_key, kayak_key in STRUCTURAL_MAP.items():
            val = str(fields.get(kayak_key) or "").strip()
            result[ss_key] = val if val else _structural_default(ss_key)

        # Numeric fields
        # FIX (KV05): For "Relation", also accept "relation_to_player" as a fallback key.
        # entity.txt files edited manually sometimes use that name instead of "relation".
        # The write path always normalises back to "relation", so this self-heals on save.
        _RELATION_ALIASES = ("relation", "relation_to_player")
        for ss_key, kayak_key in NUMERIC_MAP.items():
            try:
                if kayak_key == "relation":
                    raw = next(
                        (fields[k] for k in _RELATION_ALIASES if fields.get(k) not in (None, "", "0", 0)),
                        fields.get(kayak_key, 0),
                    )
                else:
                    raw = fields.get(kayak_key, 0)
                result[ss_key] = int(raw)
            except (ValueError, TypeError):
                result[ss_key] = 0

        # Content fields (can contain Kayak markers, passed through as-is)
        for ss_key, kayak_key in CONTENT_MAP.items():
            result[ss_key] = str(fields.get(kayak_key) or "").strip()

        # Internal metadata
        for ss_key, kayak_key in INTERNAL_FIELD_MAP.items():
            val = str(fields.get(kayak_key) or "").strip()
            if val:
                result[ss_key] = val

        # Traits
        traits = {}
        for kayak_key in TRAIT_FIELDS:
            val = str(fields.get(kayak_key) or "").strip()
            if val:
                traits[kayak_key.capitalize()] = val
        result["Traits"] = traits

        # Dialogue
        result["ConversationHistory"] = list(history) if history else []
        result["_has_dialogue"] = "1" if history else str(result.get("_has_dialogue") or "0")

        # Internal metadata (not sent to DLL, used by server-side logic)
        original = str(fields.get("original_name") or "").strip()
        if original:
            result["_original_name"] = original

        return result

    # ─── INTERNAL: DIALOGUE WRITE ───────────────────────────────────────

    def _write_dialogue(self, ss_dict: dict, campaign: Optional[str]) -> None:
        """Write dialogue via bridge hub (separate from entity.txt)."""
        from bridges.sentient_sands.bridge import _to_folder_name
        name = ss_dict.get("Name", "")
        target_id = str(ss_dict.get("ID") or ss_dict.get("persistent_id") or "").strip()
        history = list(ss_dict.get("ConversationHistory") or [])

        payload = {
            "lines": history,
            "keep_lines": self._dialogue_limit,
        }
        if name:
            payload["target_npc"] = _to_folder_name(name)
        if target_id:
            payload["target_npc_id"] = target_id
        if campaign:
            payload["campaign"] = campaign

        try:
            self._bridge.hub.replace_dialogue(payload)
        except Exception as e:
            log.debug(f"GATEWAY-DIALOGUE: replace_dialogue failed for {name}: {e}")

    # ─── INTERNAL: CACHE ────────────────────────────────────────────────

    @staticmethod
    def _cache_key(name: str, persistent_id: Optional[str] = None) -> str:
        """Build a stable cache key. Combines name + id to prevent collisions
        between different NPCs that share a display name."""
        n = str(name or "").strip()
        pid = str(persistent_id or "").strip()
        if pid and pid != n:
            return f"{n}::{pid}"
        return n

    def _cache_put(self, key: str, data: dict) -> None:
        """Insert into LRU cache, evicting oldest if at capacity."""
        if not key:
            return
        with self._cache_lock:
            self._cache.pop(key, None)  # refresh position
            self._cache[key] = dict(data)  # store a copy
            while len(self._cache) > self._max_cache:
                oldest = next(iter(self._cache))
                self._cache.pop(oldest, None)
                self._dirty_sigs.pop(oldest, None)

    # ─── INTERNAL: DIRTY CHECK ──────────────────────────────────────────

    @staticmethod
    def _content_signature(ss_dict: dict) -> str:
        """
        Hash the mutable content fields for dirty checking.

        Structural fields (Race, Faction, etc.), content fields (Personality,
        Backstory), traits, and dialogue tail are all included. If any of
        these change, the signature changes and the next write() proceeds.
        """
        parts = []

        # Structural
        for key in ("Race", "Sex", "Faction", "OriginFaction", "Job"):
            parts.append(str(ss_dict.get(key, "")))

        # Numeric
        parts.append(str(ss_dict.get("Relation", 0)))

        # Content
        for key in ("Personality", "Backstory", "SpeechQuirks"):
            parts.append(str(ss_dict.get(key, "")))

        # Internal metadata
        parts.append(str(ss_dict.get("_profile_state", "")))
        parts.append(str(ss_dict.get("_has_dialogue", "")))

        # Traits
        traits = ss_dict.get("Traits", {})
        if isinstance(traits, dict):
            for t in ("Loyalty", "Religion", "Outlook", "Motivation"):
                parts.append(str(traits.get(t, "")))

        # Dialogue tail (last 2 lines + length)
        history = ss_dict.get("ConversationHistory", [])
        parts.append(str(len(history)))
        if history:
            parts.append(str(history[-1]))
            if len(history) >= 2:
                parts.append(str(history[-2]))

        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


# ─── DEFAULTS ────────────────────────────────────────────────────────────────

def _structural_default(ss_key: str) -> str:
    """Default value for a structural SS field when Kayak returns empty."""
    _defaults = {
        "Name": "",
        "Race": "Unknown",
        "Sex": "Unknown",
        "Faction": "Unknown",
        "OriginFaction": "Unknown",
        "Job": "None",
    }
    return _defaults.get(ss_key, "")
