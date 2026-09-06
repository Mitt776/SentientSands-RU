"""
SentientSands Character Registry

Fast in-memory index of NPC name → persistent_id mappings.
Synced from Kayak on campaign load, updated instantly on NPC creation.

NO disk writes. NO HTTP calls for lookups. Pure cache layer.

This solves the performance problem:
- Legacy: Store IDs in filenames or JSON fields
- New: Store IDs inside Kayak entity.txt, but need fast lookups for SentientSands generation pipeline

Usage:
  registry.sync_from_kayak(campaign_name)  # On campaign load
  registry.get_id(npc_name)                 # Fast lookup: name → ID
  registry.register(npc_name, persistent_id) # On NPC creation
  registry.bulk_register(name_id_pairs)     # On batch creation
"""

import threading
from typing import Optional, Dict, List, Tuple

_lock = threading.RLock()
_registry: Dict[str, str] = {}  # name (lowercase) → persistent_id
_initialized = False


def sync_from_kayak(kayak_bridge, campaign_name: str) -> int:
    """
    Load all NPCs from Kayak index and populate the registry.
    Called on campaign load.
    
    Returns: number of NPCs indexed
    """
    global _registry, _initialized
    
    with _lock:
        _registry.clear()
        
        if not kayak_bridge or not kayak_bridge.is_alive():
            _initialized = False
            return 0
        
        try:
            # Get all NPC entities from Kayak
            # This assumes hub has list_entities() or similar
            # If not, we'll need to add it to hub.py
            fields = kayak_bridge.get_all_npc_fields(campaign_name)
            
            count = 0
            if isinstance(fields, dict):
                for npc_name, field_dict in fields.items():
                    persistent_id = field_dict.get('persistent_id') or field_dict.get('id')
                    if persistent_id:
                        _registry[_normalize_name(npc_name)] = str(persistent_id)
                        count += 1
            
            _initialized = True
            return count
            
        except Exception as e:
            import logging
            logging.warning(f"Character registry sync failed: {e}")
            _initialized = False
            return 0


def get_id(npc_name: str) -> Optional[str]:
    """
    Fast lookup: get persistent_id for an NPC name.
    Returns None if not found.
    
    This is the critical path — called during:
    - Character data lookup
    - Grid initialization
    - Batch profile generation
    """
    if not npc_name:
        return None
    
    with _lock:
        return _registry.get(_normalize_name(npc_name))


def register(npc_name: str, persistent_id: str) -> None:
    """
    Register a single NPC after creation.
    Called by bridge after write_npc_profile().
    """
    if not npc_name or not persistent_id:
        return
    
    with _lock:
        _registry[_normalize_name(npc_name)] = str(persistent_id)


def bulk_register(name_id_pairs: List[Tuple[str, str]]) -> None:
    """
    Register multiple NPCs at once.
    Called after batch character generation.
    
    Example:
        bulk_register([
            ("Paladin_Tealc", "uuid-1234"),
            ("Rebel_Ally_1", "uuid-5678"),
        ])
    """
    with _lock:
        for name, persistent_id in name_id_pairs:
            if name and persistent_id:
                _registry[_normalize_name(name)] = str(persistent_id)


def get_all() -> Dict[str, str]:
    """
    Get the entire registry (for debugging/export).
    """
    with _lock:
        return dict(_registry)


def clear() -> None:
    """
    Clear the registry (call on campaign switch or shutdown).
    """
    global _initialized
    with _lock:
        _registry.clear()
        _initialized = False


def is_initialized() -> bool:
    """Check if registry has been synced from Kayak."""
    with _lock:
        return _initialized


# ─── INTERNAL ────────────────────────────────────────────────────────────


def _normalize_name(name: str) -> str:
    """
    Normalize NPC name for consistent lookup.
    Case-insensitive, preserve spaces/underscores.
    """
    return str(name).strip().lower()
