# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak/bridges/sentient_sands/hub.py — Lightweight coordinator.

Routes SentientSands requests to Kayak HTTP server.
Owns error handling and fallback logic.

This is NOT a full adapter layer —it's a thin router that says:
  "Here's the SentientSands request → call Kayak endpoint → return result"

All decision logic stays in the bridge.py (what to write, what to retrieve).
All execution stays in Kayak server (indexing, retrieval, assembly).

This file just handles:
  1. HTTP client initialization
  2. Request retry logic (if Kayak dies and restarts)
  3. Error normalization
  4. Logging
"""

import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("kayak.ss.hub")

# Endpoints that legitimately take a long time. Everything else must answer
# within the default timeout or be treated as a hung server.
_SLOW_ENDPOINTS = {
    "/campaign/switch": 60.0,
    "/campaign/reload_index": 60.0,
    "/campaign/create": 60.0,
    "/entity/all_npcs": 20.0,
}

# Calls whose payload is gone for good if the breaker swallows them. A lost
# read just degrades the prompt; a lost write silently drops dialogue.
_WRITE_PREFIXES = (
    "/write/", "/rename/", "/entity/clear_fields",
    "/dialogue/save", "/dialogue/replace", "/dialogue/cull_future",
)


class KayakHub:
    """Thin HTTP client router to Kayak server. No business logic."""

    def __init__(self, kayak_url: str = "http://127.0.0.1:5001", timeout: float = 5.0):
        self.kayak_url = kayak_url.rstrip("/")
        self.timeout = timeout
        self._last_status = None
        self._last_check = 0.0
        # Circuit breaker state
        self._circuit_broken = False
        self._consecutive_failures = 0
        self._max_failures = 3

    def is_alive(self, retry_secs: float = 5.0, force: bool = False) -> bool:
        """
        Health check. Returns True if Kayak is reachable.
        Throttles checks to avoid hammering the server (default: once per 5s).
        Throttles significantly more (30s) if circuit is already broken.
        """
        now = time.monotonic()
        # If the circuit is broken, we should be much more cautious about retrying
        # unless forced by the health monitor thread.
        current_retry = 30.0 if getattr(self, "_circuit_broken", False) else retry_secs
        
        if not force and now - self._last_check < current_retry:
            return self._last_status is True
        
        self._last_check = now
        try:
            resp = requests.get(f"{self.kayak_url}/status", timeout=2.0)
            self._last_status = resp.status_code == 200
            if self._last_status:
                self._circuit_broken = False
                self._consecutive_failures = 0
            return self._last_status
        except Exception:
            self._last_status = False
            return False

    def _note_failure(self, endpoint: str, what: str, detail: str = "") -> None:
        """Count one dead call towards the breaker.

        A hung Kayak accepts the connection and then says nothing, so it never
        raises ConnectionError. Counting only refused connections meant the
        breaker could not trip in exactly the case it exists for: every call
        would sit out the full timeout, forever.
        """
        self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
        if self._consecutive_failures >= getattr(self, "_max_failures", 3):
            if not getattr(self, "_circuit_broken", False):
                log.warning(
                    f"Kayak {endpoint}: {what} {self._consecutive_failures} times in a row. "
                    f"Tripping circuit breaker."
                )
                self._circuit_broken = True
        elif detail:
            log.debug(f"Kayak {endpoint}: {what} ({detail})")

    def _post(self, endpoint: str, payload: Dict[str, Any],
              timeout: float = None) -> Dict[str, Any]:
        """
        POST to Kayak endpoint. Returns response JSON.
        Raises on network/timeout errors (caller decides fallback).
        """
        if getattr(self, "_circuit_broken", False):
            if endpoint.startswith(_WRITE_PREFIXES):
                log.warning(
                    f"Kayak {endpoint}: circuit breaker is open, so this write was "
                    f"DROPPED, not queued. The data it carried is lost."
                )
            return {"status": "fallback", "error": "circuit breaker tripped"}

        effective_timeout = timeout or _SLOW_ENDPOINTS.get(endpoint, self.timeout)
        try:
            url = f"{self.kayak_url}{endpoint}"
            resp = requests.post(url, json=payload, timeout=effective_timeout)
            
            # Successful connection, reset error counters
            self._consecutive_failures = 0
            self._circuit_broken = False

            try:
                result = resp.json()
            except ValueError:
                body = (resp.text or "").strip()
                snippet = body[:160] if body else "<empty body>"
                log.warning(
                    f"Kayak {endpoint}: non-JSON response (HTTP {resp.status_code}) — {snippet}"
                )
                return {"error": "non-json response", "status_code": resp.status_code}

            if resp.status_code >= 400:
                log.warning(f"Kayak {endpoint}: HTTP {resp.status_code} — {result.get('error', 'unknown')}")
            return result
        except requests.Timeout:
            log.error(f"Kayak {endpoint}: timeout after {effective_timeout}s")
            self._note_failure(endpoint, "timed out")
            raise
        except requests.ConnectionError as e:
            self._note_failure(endpoint, "connection refused", str(e))
            raise
        except Exception as e:
            log.error(f"Kayak {endpoint}: {type(e).__name__}: {e}")
            raise

    def reset_breaker(self):
        """Manually reset the circuit breaker (called by health monitor)."""
        self._circuit_broken = False
        self._consecutive_failures = 0
        log.info("Kayak: Circuit breaker reset.")

    # ─── CAMPAIGN ───────────────────────────────────────────────────────

    def switch_campaign(self, campaign_name: str, ss_campaign_dir: Optional[str] = None) -> bool:
        """
        Switch active campaign (create-if-needed).
        If ss_campaign_dir provided, also syncs player bio.
        Returns True if successful, False otherwise.
        """
        for attempt in range(30):
            if self.is_alive(force=True):
                break
            time.sleep(0.5)

        try:
            result = self._post("/campaign/switch", {"name": campaign_name})
            if "error" in result:
                log.error(f"write_campaign_switch failed: {result['error']}")
                return False
            if ss_campaign_dir and hasattr(self, '_bridge'):
                # If this hub has a bridge attached, sync player bio immediately
                pass
            return True
        except Exception as e:
            log.error(f"switch_campaign exception: {e}")
            return False

    # ─── WRITES ─────────────────────────────────────────────────────────

    def write_npc_profile(self, payload: Dict[str, Any]) -> bool:
        """POST entity.txt for character profile (triggers reindex)."""
        try:
            result = self._post("/write/npc", payload)
            if "error" in result:
                log.debug(f"write_npc_profile: {result['error']}")
                return False
            return result.get("status") == "ok"
        except Exception as e:
            log.debug(f"write_npc_profile exception: {e}")
            return False

    def write_stats(self, payload: Dict[str, Any]) -> bool:
        """POST stats.txt (no reindex, read fresh at prompt-time)."""
        try:
            result = self._post("/write/stats", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    def write_dialogue(self, payload: Dict[str, Any]) -> bool:
        """POST dialogue exchange (append + trim)."""
        try:
            result = self._post("/dialogue/save", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    def replace_dialogue(self, payload: Dict[str, Any]) -> bool:
        """Replace a full dialogue history for one entity."""
        try:
            result = self._post("/dialogue/replace", payload)
            if result.get("status") == "ok":
                return True

            # Entity genuinely doesn't exist in Kayak yet — no point falling back
            # to /dialogue/save either, since there's no entity to attach it to.
            _err = str(result.get("error") or "").lower()
            if "not found" in _err and "status_code" not in result:
                return False

            # Compatibility fallback: older Kayak builds may not expose /dialogue/replace,
            # returning 404 or a non-JSON response. Persist only the latest exchange
            # through /dialogue/save so dialogue isn't silently lost.
            _is_non_json = result.get("error") == "non-json response"
            _is_route_404 = result.get("status_code") == 404
            if _is_non_json or _is_route_404:
                lines = payload.get("lines") or []
                player_line, npc_line = self._extract_latest_exchange(lines)
                if not player_line and not npc_line:
                    return False

                save_payload = {}
                for key in ("target_npc", "target_npc_id", "persistent_id", "runtime_id", "campaign"):
                    if payload.get(key):
                        save_payload[key] = payload[key]
                save_payload["player_line"] = player_line
                save_payload["npc_line"] = npc_line
                return self.write_dialogue(save_payload)
            return False
        except Exception:
            return False

    def cull_future_dialogue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Cull dialogue lines after a supplied in-game timestamp."""
        try:
            return self._post("/dialogue/cull_future", payload)
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def write_mandatory_file(self, payload: Dict[str, Any]) -> bool:
        """POST mandatory file (e.g., player bio)."""
        try:
            result = self._post("/write/mandatory", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    def write_entity_field(self, payload: Dict[str, Any]) -> bool:
        """PATCH-like field update for an existing entity."""
        try:
            result = self._post("/write/entity_field", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    def rename_npc(self, payload: Dict[str, Any]) -> bool:
        """Rename an NPC entity folder and sync name fields."""
        try:
            result = self._post("/rename/npc", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    def clear_entity_fields(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Blank existing entity field values without removing the fields."""
        try:
            return self._post("/entity/clear_fields", payload)
        except Exception as e:
            return {"error": str(e)}

    def set_config_value(self, key: str, value: Any) -> bool:
        """Update a live Kayak config value."""
        try:
            result = self._post("/config/set", {"key": key, "value": value})
            return result.get("status") == "ok"
        except Exception:
            return False

    def get_config_value(self, key: str) -> Any:
        """Read one live Kayak config value."""
        try:
            result = self._post("/config/get", {"key": key})
            return result.get("value")
        except Exception:
            return None

    # ─── READS ──────────────────────────────────────────────────────────

    def read_npc_fields(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """GET NPC's entity fields (reads entity.txt)."""
        try:
            result = self._post("/entity/fields", payload)
            fields = result.get("fields", {}) or {}
            # Preserve resolved entity folder name for callers that need stable
            # identity across display-name renames.
            if result.get("found") and result.get("name"):
                fields = dict(fields)
                fields["_entity_name"] = str(result.get("name"))
                if result.get("category"):
                    fields["_entity_category"] = str(result.get("category"))
            return fields
        except Exception:
            return {}

    def read_dialogue(self, payload: Dict[str, Any]) -> list:
        """GET NPC's dialogue history."""
        try:
            result = self._post("/dialogue/read", payload)
            return result.get("lines", [])
        except Exception:
            return []

    # ─── PROMPT BUILDING ────────────────────────────────────────────────

    def build_chat_prompt(self, payload: Dict[str, Any]) -> str:
        """POST /prompt/chat. Returns assembled prompt or empty string on error."""
        try:
            result = self._post("/prompt/chat", payload)
            return result.get("prompt", "")
        except Exception:
            log.debug("build_chat_prompt failed, returning empty")
            return ""

    def build_speak_prompt(
        self,
        line: str,
        target_npc: Optional[str] = None,
        campaign: Optional[str] = None,
    ) -> str:
        """POST /prompt/speak for direct echo speech."""
        try:
            payload = {"line": line}
            if target_npc:
                payload["target_npc"] = target_npc
            if campaign:
                payload["campaign"] = campaign
            result = self._post("/prompt/speak", payload)
            return result.get("prompt", "")
        except Exception:
            return ""

    def build_loremaster_prompt(self, events: str, campaign: Optional[str] = None) -> str:
        """POST /prompt/loremaster for world narrative."""
        try:
            payload = {"events": events}
            if campaign:
                payload["campaign"] = campaign
            result = self._post("/prompt/loremaster", payload)
            return result.get("prompt", "")
        except Exception:
            return ""

    def build_radiant_prompt(self, payload: Dict[str, Any]) -> str:
        """POST /prompt/radiant. Returns assembled prompt or empty string on error."""
        try:
            result = self._post("/prompt/radiant", payload)
            return result.get("prompt", "")
        except Exception:
            log.debug("build_radiant_prompt failed, returning empty")
            return ""

    def build_biography_prompt(
        self,
        npc_data: str,
        campaign: Optional[str] = None,
        race: Optional[str] = None,
        persona_category: Optional[str] = None,
    ) -> str:
        """POST /prompt/biography for NPC bio generation."""
        try:
            payload = {"npc_data": npc_data}
            if campaign:
                payload["campaign"] = campaign
            if race:
                payload["race"] = race
            if persona_category:
                payload["persona_category"] = persona_category
            result = self._post("/prompt/biography", payload)
            return result.get("prompt", "")
        except Exception:
            return ""

    # ─── WORLD EVENTS ───────────────────────────────────────────────────

    def write_world_event(self, event_name: str, event_content: str, campaign: Optional[str] = None) -> bool:
        """POST world event (rumor, world event, etc.)."""
        try:
            payload = {"name": event_name, "entity_content": event_content}
            if campaign:
                payload["campaign"] = campaign
            result = self._post("/write/world_event", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    # ─── CHARACTER REGISTRY / INDEX ──────────────────────────────────────

    def get_all_npc_fields(self, campaign: Optional[str] = None) -> Dict[str, Dict[str, str]]:
        """
        Retrieve ALL NPC entities and their fields from Kayak index.
        Used by character registry to sync name → persistent_id mappings.
        
        Returns:
            {"NPC_Name": {"persistent_id": "...", "faction": "...", ...}, ...}
            Or empty dict on error.
        """
        try:
            payload = {}
            if campaign:
                payload["campaign"] = campaign
            result = self._post("/entity/all_npcs", payload)
            return result.get("npcs", {})
        except Exception:
            log.debug("get_all_npc_fields: endpoint not available, registry will be empty on sync")
            return {}

    def write_npc_knowledge(
        self,
        target_npc: str,
        location: str,
        campaign: Optional[str] = None,
        target_npc_id: Optional[str] = None,
    ) -> bool:
        """
        Add a location to an NPC's $knows_about field.
        Called when NPC experiences a new location during dialogue.
        """
        try:
            payload = {"target_npc": target_npc, "location": location}
            if target_npc_id:
                payload["target_npc_id"] = target_npc_id
            if campaign:
                payload["campaign"] = campaign
            result = self._post("/write/npc_knowledge", payload)
            return result.get("status") == "ok"
        except Exception:
            return False

    @staticmethod
    def _extract_latest_exchange(lines: Any) -> tuple[str, str]:
        """Best-effort extraction of the latest player/npc lines from history."""
        if not isinstance(lines, list):
            return "", ""

        cleaned = [str(l).strip() for l in lines if str(l).strip()]
        if not cleaned:
            return "", ""

        # Remove time tags like "[Day 58, 10:53] " before parsing speaker/message.
        def _strip_time(s: str) -> str:
            out = s.strip()
            while out.startswith("["):
                m = __import__("re").match(r"^\[[^\]]+\]\s*", out)
                if not m:
                    break
                out = out[m.end():].lstrip()
            return out

        # Take the last two non-empty lines as a pragmatic fallback.
        if len(cleaned) == 1:
            single = _strip_time(cleaned[0])
            if ":" in single:
                return single.split(":", 1)[1].strip(), ""
            return single, ""

        prev = _strip_time(cleaned[-2])
        last = _strip_time(cleaned[-1])

        def _msg_only(s: str) -> str:
            return s.split(":", 1)[1].strip() if ":" in s else s

        return _msg_only(prev), _msg_only(last)
