"""
SentientSands Character Creation Handler

Orchestrates the complete NPC creation workflow:
  1. Generate character profile (existing SentientSands functions)
  2. Write to Kayak via bridge
  3. Update character registry
  4. Return persistent_id to SentientSands

Designed for integration with kenshi_llm_server.py's character generation code.
Fast, lightweight, no extra disk files.

Usage in kenshi_llm_server.py:
  
  from scripts import ss_character_creation_handler
  
  # Single NPC:
  persistent_id = ss_character_creation_handler.create_from_profile(
      profile_dict,
      npc_name="Paladin_Tealc",
      campaign=ACTIVE_CAMPAIGN
  )
  
  # Batch:
  results = ss_character_creation_handler.create_batch(
      [profile1, profile2, ...],
      campaign=ACTIVE_CAMPAIGN
  )
  # results = [("NPC_1", "uuid-1"), ("NPC_2", "uuid-2"), ...]
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ss.creation")


def _resolve_registry_module():
    try:
        import ss_character_registry as registry_module
        return registry_module
    except ImportError:
        try:
            from . import ss_character_registry as registry_module
            return registry_module
        except ImportError:
            return None


def create_from_profile(
    profile: Dict[str, Any],
    npc_name: str,
    campaign: Optional[str] = None,
    kayak_bridge=None,
    registry=None,
) -> Optional[str]:
    """
    Create a single NPC from SentientSands profile and register it.
    
    Args:
        profile: Character profile dict (Personality, Backstory, Race, Sex, etc.)
        npc_name: NPC display name (e.g. "Paladin_Tealc")
        campaign: Kayak campaign name
        kayak_bridge: SentientSandsBridge instance (auto-imported if None)
        registry: Character registry (auto-imported if None)
    
    Returns:
        persistent_id (UUID) if successful, None on error
    
    IMPORTANT: Call this from kenshi_llm_server after LLM generates profile.
    """
    if kayak_bridge is None:
        # Import here to avoid circular dependencies
        try:
            import sys
            import os
            # Try to get the global kayak instance from kenshi_llm_server
            # This is a bit fragile but avoids hard imports
            frame = sys._getframe(1)
            kayak_bridge = frame.f_globals.get("kayak")
            if not kayak_bridge:
                log.error("create_from_profile: kayak_bridge not available (Kayak not enabled?)")
                return None
        except Exception as e:
            log.warning(f"create_from_profile: could not resolve kayak_bridge: {e}")
            return None
    
    if registry is None:
        registry = _resolve_registry_module()
        if registry is None:
            log.warning("create_from_profile: ss_character_registry not available")

    if not kayak_bridge:
        log.error("create_from_profile: Kayak is not enabled")
        return None
    
    # Ensure profile has a Name field
    if "Name" not in profile:
        profile["Name"] = npc_name

    if not profile.get("_persona_category"):
        try:
            from ss_identity import get_persona_category
            profile["_persona_category"] = get_persona_category(
                profile.get("Race", ""),
                profile.get("Faction", ""),
                name=npc_name,
                source="creation_handler",
            )
        except Exception as _pc_err:
            log.debug(f"create_from_profile: persona category derive failed (non-fatal): {_pc_err}")

    # KV05: Populate faction-flavored traits (Loyalty, Religion, Outlook, Motivation)
    # if the profile doesn't already have them from LLM generation.
    try:
        from personality_rules import generate_npc_traits
        if not profile.get("Traits"):
            profile["Traits"] = generate_npc_traits(
                faction=profile.get("Faction", ""),
                race=profile.get("Race", ""),
                origin_faction=profile.get("OriginFaction", ""),
            )
    except Exception as _te:
        log.debug(f"create_from_profile: trait generation failed (non-fatal): {_te}")

    # Write to Kayak
    try:
        persistent_id = kayak_bridge.create_npc_entity(
            profile,
            campaign=campaign
        )
        
        if not persistent_id:
            log.error(f"create_from_profile: bridge.create_npc_entity returned None for {npc_name}")
            return None
        
        # Register in memory (fast)
        if registry:
            try:
                registry.register(npc_name, persistent_id)
            except Exception as e:
                log.warning(f"create_from_profile: registry.register failed: {e}")
        
        log.info(f"Created + registered NPC: {npc_name} (id={persistent_id})")
        return persistent_id
        
    except Exception as e:
        log.error(f"create_from_profile: {npc_name}: {e}")
        return None


def create_batch(
    profiles: List[Dict[str, Any]],
    campaign: Optional[str] = None,
    kayak_bridge=None,
    registry=None,
) -> List[Tuple[str, str]]:
    """
    Create multiple NPCs in batch (parallel to minimize latency).
    
    Args:
        profiles: List of character profile dicts (each should have 'Name')
        campaign: Kayak campaign name
        kayak_bridge: SentientSandsBridge instance
        registry: Character registry
    
    Returns:
        List of (npc_name, persistent_id) tuples
    
    IMPORTANT: Call after batch profile generation completes.
    Handles all the parallel HTTP calls internally.
    """
    if not profiles:
        return []
    
    if kayak_bridge is None:
        try:
            frame = __import__("sys")._getframe(1)
            kayak_bridge = frame.f_globals.get("kayak")
            if not kayak_bridge:
                log.error("create_batch: kayak_bridge not available")
                return []
        except Exception as e:
            log.warning(f"create_batch: could not resolve kayak_bridge: {e}")
            return []
    
    if registry is None:
        registry = _resolve_registry_module()

    if not kayak_bridge:
        log.error("create_batch: Kayak is not enabled")
        return []
    
    # Use bridge's batch operation for efficiency
    try:
        results = kayak_bridge.create_batch_npc_entities(profiles, campaign=campaign)
        
        # Register all at once
        if registry and results:
            try:
                registry.bulk_register(results)
            except Exception as e:
                log.warning(f"create_batch: registry.bulk_register failed: {e}")
        
        log.info(f"Batch completed: created {len(results)} NPCs")
        return results
        
    except Exception as e:
        log.error(f"create_batch: {e}")
        return []


def sync_registry_from_kayak(
    campaign: Optional[str] = None,
    kayak_bridge=None,
    registry=None,
) -> int:
    """
    Sync the character registry from Kayak (on campaign load).
    
    Args:
        campaign: Kayak campaign name
        kayak_bridge: SentientSandsBridge instance
        registry: Character registry module
    
    Returns:
        Number of NPCs indexed
    
    CALL THIS ONCE PER CAMPAIGN LOAD (e.g., in switch_campaign).
    """
    if kayak_bridge is None:
        try:
            frame = __import__("sys")._getframe(1)
            kayak_bridge = frame.f_globals.get("kayak")
        except Exception:
            kayak_bridge = None
    
    if registry is None:
        registry = _resolve_registry_module()
        if registry is None:
            return 0

    if not kayak_bridge or not registry:
        log.warning("sync_registry_from_kayak: missing kayak_bridge or registry")
        return 0
    
    try:
        count = registry.sync_from_kayak(kayak_bridge, campaign)
        log.info(f"Character registry synced: {count} NPCs indexed from Kayak")
        return count
    except Exception as e:
        log.error(f"sync_registry_from_kayak: {e}")
        return 0


def get_npc_id(npc_name: str, registry=None) -> Optional[str]:
    """
    Fast lookup: get persistent_id for an NPC.
    
    Returns:
        persistent_id if registered, None otherwise
    
    FAST PATH: O(1) lookup, no Kayak calls.
    """
    if registry is None:
        registry = _resolve_registry_module()
        if registry is None:
            return None
    
    if not registry:
        return None
    
    try:
        return registry.get_id(npc_name)
    except Exception as e:
        log.warning(f"get_npc_id({npc_name}): {e}")
        return None
