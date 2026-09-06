"""
ss_prompt.py — Prompt assembly for SentientSands.

Builds the text that goes to the LLM: NPC condition strings, player
status and inventory, dynamic lore injection, event blocks, and the
full system prompt. Pure assembly — reads server globals, returns strings.
No disk I/O beyond prompt template loading. No LLM calls.

Server globals accessed via _sv():
  _sv().SHOP_STOCK, _sv().LORE_DATABASE, _sv().ACTIVE_CAMPAIGN, _sv().EVENT_HISTORY,
  _sv().TEMPLATES_DIR, _sv()._COMPONENT_CACHE, _sv().WORLD_INDEX

PLAYER_CONTEXT is imported directly from ss_identity (it lives there).

Extracted from kenshi_llm_server.py — [Design: Pineaxe]
"""

import logging
import os
import re
import sys

from ss_identity import (
    PLAYER_CONTEXT,
    get_effective_player_context,
    resolve_live_context,
)
from personality_rules import build_loyalty_note


# ─── PROMPT PERMANENT CACHE (Internal) ───────────────────────────────────────

_RUMORS_CACHE = []
_RUMORS_CACHE_MTIME = 0.0


# ─── LAZY SERVER STATE ACCESSOR ──────────────────────────────────────────────

def _sv():
    """Return the live server module state (prefer __main__ when script-launched)."""
    _main = sys.modules.get("__main__")
    if _main and hasattr(_main, "ACTIVE_CAMPAIGN") and hasattr(_main, "SHOP_STOCK") and hasattr(_main, "LORE_DATABASE"):
        _main_file = str(getattr(_main, "__file__", "")).replace("\\", "/").lower()
        if _main_file.endswith("/kenshi_llm_server.py"):
            return _main
    _loaded = sys.modules.get("kenshi_llm_server")
    if _loaded and hasattr(_loaded, "ACTIVE_CAMPAIGN") and hasattr(_loaded, "SHOP_STOCK") and hasattr(_loaded, "LORE_DATABASE"):
        return _loaded
    import kenshi_llm_server as _s
    return _s


# ─── PROMPT FUNCTIONS ────────────────────────────────────────────────────────

def extract_location_context(context=None):
    """Extract live town/region/location flags from a context dict."""
    ctx = context if isinstance(context, dict) else (PLAYER_CONTEXT if isinstance(PLAYER_CONTEXT, dict) else {})
    env = ctx.get("environment", {}) if isinstance(ctx, dict) else {}
    if not isinstance(env, dict):
        env = {}

    town = str(
        env.get("town_name")
        or env.get("town")
        or ctx.get("town_name")
        or ctx.get("town")
        or ""
    ).strip()
    region = str(
        env.get("biome")
        or env.get("region")
        or ctx.get("biome")
        or ctx.get("region")
        or ""
    ).strip()
    indoors = bool(env.get("indoors"))
    in_town = bool(env.get("in_town")) or bool(town)

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
        "indoors": indoors,
        "in_town": in_town,
        "known": bool(tag),
        "tag": tag,
    }


def format_location_summary(context=None):
    """Return a compact town/region label for prompt use."""
    loc = extract_location_context(context)
    return loc.get("tag", "")

def build_detailed_context_string(npc_name, char_data=None, live_ctx=None):
    # Try to get live context for this specific NPC
    ctx = live_ctx
    if ctx is None:
        _, ctx = resolve_live_context(name=npc_name, context=char_data, explicit_id=(char_data or {}).get("ID") if char_data else None)
    
    if not ctx:
        if not char_data:
            return ""
        # If no live context, fallback to persistent char_data
        ctx = char_data
    
    lines = [f"CURRENT CONDITION of {npc_name}:"]

    # --- Character State (imprisoned / enslaved / escaped) ---
    char_state = ctx.get("character_state", "normal")
    is_incapacitated = ctx.get("is_incapacitated", False)
    state_labels = {
        "imprisoned":     f"CRITICAL: {npc_name} is currently IMPRISONED. They are locked up and cannot move freely. They should speak with desperation, resignation, or defiance.",
        "enslaved":       f"CRITICAL: {npc_name} is ENSLAVED and wearing shackles. They are bound to a master. They should speak with fear, exhaustion, or suppressed rage.",
        "escaped-slave":  f"CRITICAL: {npc_name} is an ESCAPED SLAVE — no longer chained but hunted. They should be paranoid, guarded, and desperate.",
        "unconscious":    f"CRITICAL: {npc_name} is UNCONSCIOUS and cannot speak.",
        "dead":           f"CRITICAL: {npc_name} is DEAD.",
    }
    if char_state in state_labels:
        lines.append(state_labels[char_state])

    # Identity
    race = ctx.get("race") or ctx.get("Race", "Unknown")
    gender = ctx.get("gender") or ctx.get("Sex", "Unknown")
    faction = ctx.get("faction") or ctx.get("Faction", "Unknown")
    job = ctx.get("job") or ctx.get("Job", "None")
    money = ctx.get("money") or 0
    relation = ctx.get("relation")

    lines.append(f"- RACE: {race}")
    lines.append(f"- SEX: {gender}")
    lines.append(f"- FACTION: {faction}")
    
    # Shopkeeper / Trader Status
    # Trust only the DLL trader flag for merchant behavior.
    # `in_shop` is informational only because bars also set it.
    is_trader = ctx.get("is_trader", False)
    in_shop = ctx.get("in_shop", False)
    building_name = ctx.get("building_name", "Unknown")
    
    if is_trader:
        shop_note = f"ROLE: {npc_name} is a SHOPKEEPER/TRADER."
        if in_shop:
            shop_note += f" They are currently IN THEIR SHOP ({building_name})."
        shop_note += " They are authorized to sell items and cats from their inventory in exchange for the player's cats or items."
        _stock_helper = getattr(_sv(), "get_shop_stock_for_npc", None)
        if callable(_stock_helper):
            _stock_items = _stock_helper(npc_name) or []
        else:
            _stock_items = _sv().SHOP_STOCK.get(npc_name, [])
        if _stock_items:
            shop_note += f"\nSHOP ITEM RULE: Use ONLY the exact item names from your SHOP STOCK list below in [ACTION: GIVE_ITEM: ...]. Do NOT invent or abbreviate item names."
        else:
            shop_note += "\nSHOP ITEM RULE: Exact shop stock is unavailable right now. You may speak generally about your wares, but do NOT promise or hand over a specific item unless it appears in an explicit SHOP STOCK list."
        lines.append(shop_note)
        if _stock_items:
            lines.append("SHOP STOCK (use these exact names in [ACTION: GIVE_ITEM: ...]):")
            for _item in _stock_items:
                lines.append(f"  - {_item}")
    
    # Leader Status
    if ctx.get("is_leader", False):
        lines.append(f"ROLE: {npc_name} is the LEADER of their faction. They speak with authority and make final decisions for their group.")

    lines.append(f"- CURRENT GOAL/JOB: {job}")
    if relation is not None:
        lines.append(f"- FACTION STANDING: {relation} (Stance: {'ALLIED' if relation >= 50 else 'FRIENDLY' if relation > 0 else 'NEUTRAL' if relation == 0 else 'HOSTILE' if relation <= -30 else 'UNFRIENDLY'})")
    lines.append(f"- MONEY: {money} cats")

    # Group Leader Awareness
    player_faction = get_effective_player_context().get('faction', 'Nameless')
    lines.extend(build_loyalty_note(npc_name, faction, player_faction, ctx.get("factionID")))
    # Medical
    med = ctx.get("medical", {})
    if med:
        blood = med.get("blood", 100)
        hunger = med.get("hunger", 300)
        limbs = med.get("limbs", {})
        
        status_parts = []
        
        # Hunger Logic
        if hunger < 100: status_parts.append("STARVING")
        elif hunger < 250: status_parts.append("HUNGRY")
        else: status_parts.append("WELL FED") 
        
        # Health Logic
        max_blood = med.get("max_blood", 100)
        blood_pct = blood / max_blood if max_blood > 0 else 1.0
        blood_rate = med.get("blood_rate", 0.0)
        
        if blood_rate > 0.01:
            status_parts.append("BLEEDING")
        elif blood_pct < 0.5:
            status_parts.append("WEAK FROM BLOODLOSS")
        elif blood_pct < 0.85:
            status_parts.append("INJURED")
            
        if med.get("is_unconscious"): status_parts.append("UNCONSCIOUS")
        
        lines.append(f"- CONDITION: {', '.join(status_parts) if status_parts else 'Healthy'}")
        
        # Limb Logic
        injuries = []
        # Filter out _max keys for iteration
        base_limbs = [l for l in limbs.keys() if not l.endswith("_max")]
        for limb in base_limbs:
            hp = limbs.get(limb, 100)
            hp_max = limbs.get(f"{limb}_max", 100)
            hp_pct = hp / hp_max if hp_max > 0 else 1.0
            
            if hp <= -hp_max: 
                injuries.append(f"{limb.upper()} GONE/SEVERED")
            elif hp < 0: 
                injuries.append(f"{limb.upper()} IS CRIPPLED")
            elif hp_pct < 0.5: 
                injuries.append(f"{limb.upper()} IS INJURED")
            
        if injuries: 
            lines.append(f"- INJURIES: {', '.join(injuries)}")
        else:
            lines.append("- INJURIES: None")
    
    # Environment
    env = ctx.get("environment", {})
    if env:
        loc_ctx = extract_location_context(ctx)
        loc = []
        if loc_ctx.get("indoors"): loc.append("Indoors")
        if loc_ctx.get("town"): loc.append(f"In town ({loc_ctx.get('town')})")
        if loc_ctx.get("region"): loc.append(f"Region ({loc_ctx.get('region')})")
        if loc: lines.append(f"- LOCATION: {', '.join(loc)}")

    # Stats & Skills (Visible Power)
    stats = ctx.get("stats", {})
    if stats:
        lines.append(f"VISIBLE POWER of {npc_name}:")
        core = [f"{k[:3].upper()}: {int(float(stats.get(k, 0)))}" for k in ["strength", "dexterity", "toughness", "perception"]]
        lines.append(f"- ATTRIBUTES: {' | '.join(core)}")
        
        notable = []
        combat_skills = ["melee_attack", "melee_defence", "dodge", "katanas", "sabres", "hackers", "heavy_weapons", "blunt", "polearms", "martial_arts", "crossbows", "turrets", "stealth", "athletics"]
        for s in combat_skills:
            val = int(float(stats.get(s, 0)))
            if val > 15: # Only show competent skills
                notable.append(f"{s.replace('_', ' ').capitalize()}: {val}")
        if notable:
            lines.append(f"- NOTABLE SKILLS: {', '.join(notable)}")

    # Memories
    mem = ctx.get("memories", {})
    st = [_sv().SHORT_TERM_MEM.get(m, str(m)) for m in mem.get("short_term", [])]
    lt = [_sv().LONG_TERM_MEM.get(m, str(m)) for m in mem.get("long_term", [])]
    
    if st or lt:
        lines.append(f"PERCEPTION OF PLAYER:")
        if st: lines.append(f"- SHORT TERM: {', '.join(st)}")
        if lt: lines.append(f"- HISTORY TAGS: {', '.join(lt)}")
        
    # Inventory & Equipment (Categorized)
    inv = ctx.get("inventory", [])
    if inv:
        worn = [i for i in inv if i.get("equipped")]
        held = [i for i in inv if not i.get("equipped")]
        # Traders need broader inventory visibility for reliable item sale actions.
        held_limit = 40 if is_trader else 10
        
        if worn:
            lines.append(f"EQUIPMENT WORN by {npc_name}:")
            for item in worn:
                _name = item.get("name", "Unknown Item")
                _count = item.get("count", 1)
                _slot = str(item.get("slot", "unknown")).upper()
                _price = item.get("price")
                if _price is not None:
                    lines.append(f"- {_name} (x{_count}) [{_slot}] [value: {_price} cats]")
                else:
                    lines.append(f"- {_name} (x{_count}) [{_slot}]")
        
        if held:
            lines.append(f"INVENTORY HELD by {npc_name}:")
            for item in held[:held_limit]:
                _name = item.get("name", "Unknown Item")
                _count = item.get("count", 1)
                _price = item.get("price")
                if _price is not None:
                    lines.append(f"- {_name} (x{_count}) [value: {_price} cats]")
                else:
                    lines.append(f"- {_name} (x{_count})")
            if len(held) > held_limit:
                lines.append(f"- ... (and {len(held)-held_limit} other items)")
    else:
        lines.append(f"INVENTORY: Empty")

    # Nearby Awareness (Sensory Perception) — capped to 8 closest
    nearby = ctx.get("nearby", [])[:8]
    if nearby:
        lines.append(f"PEOPLE NEARBY (Visual Awareness):")
        for p in nearby:
            dist = float(p.get("dist", 0))
            dist_str = "Immediate proximity" if dist < 2.5 else f"{int(dist)}m away"
            p_name = p.get("name", "Someone")
            p_race = p.get("race", "Unknown")
            p_gender = p.get("gender", "Unknown")
            p_fact = p.get("faction", "Unknown")
            p_fact_display = p_fact
            if p_fact == "Nameless" or p_fact == get_effective_player_context().get('faction', 'Nameless'):
                p_fact_display = f"Player's Squad: {p_fact}"
            
            p_health = p.get("health", "Healthy")
            p_equip = p.get("equipment", "")
            
            p_desc = f"- {p_name} ({p_gender} {p_race}, {p_fact_display}) | Health: {p_health} | {dist_str}"
            if p_equip:
                p_desc += f" | Visible Gear: {p_equip}"
            lines.append(p_desc)

    return "\n".join(lines)

# load_configs() is called by server at startup — not here


def load_prompt_component(filename, default_text=""):
    def _try_cached(path, source_label):
        if not os.path.exists(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            cached = _sv()._COMPONENT_CACHE.get(path)
            if cached and cached[0] == mtime:
                return cached[1]
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                _sv()._COMPONENT_CACHE[path] = (mtime, content)
                logging.info(f"PROMPT: Loaded {filename} from {source_label}")
                return content
        except Exception as e:
            logging.error(f"Error reading {filename} from {source_label}: {e}")
        return None

    # Try active campaign first, then templates (read-only)
    result = _try_cached(os.path.join(_sv().get_campaign_dir(), filename), f"campaign:{_sv().ACTIVE_CAMPAIGN}")
    if result is not None:
        return result
    result = _try_cached(os.path.join(_sv().TEMPLATES_DIR, filename), "templates (read-only)")
    if result is not None:
        return result
    return default_text

def format_player_status(player_ctx):
    """Summarizes player vitals and faction into a readable block."""
    if not player_ctx: return "No status data."
    res = "PLAYER STATUS:\n"
    res += f"- Race: {player_ctx.get('race', 'Unknown')}\n"
    res += f"- Gender: {player_ctx.get('gender', 'male')}\n"
    med = player_ctx.get("medical", {})
    if med:
        hunger = med.get("hunger", 300)
        blood = med.get("blood", 100)
        max_blood = med.get("max_blood", 100)
        blood_pct = blood / max_blood if max_blood > 0 else 1.0
        blood_rate = med.get("blood_rate", 0.0)
        status = []
        if hunger < 80: status.append("STARVING")
        elif hunger < 200: status.append("VERY HUNGRY")
        elif hunger < 250: status.append("HUNGRY")
        
        if blood_rate > 0.01: 
            status.append("BLEEDING")
        elif blood_pct < 0.5: 
            status.append("CRITICAL BLOODLOSS")
        elif blood_pct < 0.85: 
            status.append("INJURED")
            
        res += f"- Condition: {', '.join(status) if status else 'Healthy/Fed'}\n"
    res += f"- Money: {player_ctx.get('money', 0)} cats\n"
    res += f"- Faction: {player_ctx.get('faction', 'Nameless')}\n"
    location_tag = format_location_summary(player_ctx)
    if location_tag:
        res += f"- Location: {location_tag}\n"
    return res

def format_player_inventory(player_ctx):
    """Categorizes player inventory into Visible vs Concealed for the LLM."""
    if not player_ctx: return "No inventory data."
    inv = player_ctx.get("inventory", [])
    if not inv: return "Inventory: Empty or not visible."
    
    visible = []
    bag = []
    for item in inv:
        name = item.get("name", "Unknown Item")
        count = item.get("count", 1)
        equipped = item.get("equipped", False)
        slot = item.get("slot", "none")
        display = f"{name} (x{count})"
        price = item.get("price")
        if price is not None:
            display += f" [value: {price} cats]"
        if equipped:
            visible.append(f"{display} [{slot.upper()}]")
        else:
            bag.append(display)
            
    res = "PLAYER EQUIPMENT & INVENTORY:\n"
    res += "VISIBLE (Worn/Held):\n" + ("\n".join([f"- {v}" for v in visible]) if visible else "- Nothing visible.") + "\n"
    res += "CONCEALED (In Bag/Pack):\n" + ("\n".join([f"- {b}" for b in bag[:5]]) if bag else "- Bag appears empty.")
    if len(bag) > 5:
        res += f"\n- ... and {len(bag)-5} more items."
    return res
    
def fetch_dynamic_lore(npc_data=None, env_override=None):
    """Filters the in-memory _sv().LORE_DATABASE by NPC tags, groups by type, and returns
    labeled prompt sections with per-type character budgets."""
    if not _sv().LORE_DATABASE:
        # Do NOT fall back to world_lore.txt — it is not loaded and risks exceeding context.
        return "The world is a brutal, post-apocalyptic sword-punk wasteland with no central government."

    search_terms = ["Global"]

    # 1. Build search terms from NPC context
    if npc_data:
        faction = npc_data.get("Faction", "")
        if faction and faction != "Unknown":
            search_terms.append(faction)
        search_terms.extend(p for p in npc_data.get("SourcePlatoons", []) if p)
        # Race tag enables race-specific chunks (e.g. lore_race_skeleton, lore_race_shek)
        race = npc_data.get("Race", "")
        if race and race != "Unknown":
            search_terms.append(race)
            _rl = race.lower()
            _fl = (faction or "").lower()
            _ofl = (npc_data.get("OriginFaction", "") or "").lower()
            _hive_ctx = "hive" in _fl or "hive" in _ofl
            if "fogman" in _rl or "deadhive" in _rl:
                search_terms.append("Fogman")
            elif "hive" in _rl:
                search_terms.append("Hiver")
                if "prince" in _rl:
                    search_terms.append("Hive Prince")
                if "soldier" in _rl or ("drone" in _rl and "worker" not in _rl):
                    search_terms.append("Soldier Drone")
                if "worker" in _rl:
                    search_terms.append("Hive Worker Drone")
            elif "drone" in _rl and _hive_ctx:
                # e.g. vanilla "Worker Drone" whose faction confirms Hiver identity
                search_terms.append("Hiver")
                if "worker" in _rl:
                    search_terms.append("Hive Worker Drone")
                elif "soldier" in _rl:
                    search_terms.append("Soldier Drone")
            elif "skeleton" in _rl or "mechanical" in _rl or ("drone" in _rl and not _hive_ctx):
                search_terms.append("Skeleton")
                
        # Religion tag injects theology for the NPC's actual faith regardless of faction
        # e.g. a Narkoite wanderer in a secular faction still gets Okran/Narko lore
        religion = npc_data.get("Traits", {}).get("Religion", "")
        if religion and religion not in ("N/A", "Unknown", "Hive-Bound"):
            search_terms.append(religion)

    # 2. Add location tags

    env = env_override if env_override is not None else (PLAYER_CONTEXT.get("environment", {}) if PLAYER_CONTEXT else {})

    if isinstance(env, dict):

        if env.get("town_name"): search_terms.append(env.get("town_name"))

        if env.get("biome"): search_terms.append(env.get("biome"))

    # 3. Match chunks and bucket by type
    by_type = {t: [] for t in _sv()._LORE_TYPE_ORDER}
    for chunk in _sv().LORE_DATABASE:
        if any(term in chunk.get("tags", []) for term in search_terms):
            chunk_type = chunk.get("type", "faction")
            if chunk_type in by_type:
                by_type[chunk_type].append(chunk.get("content", ""))

    # 4. Build output: labeled sections, each capped at its own budget
    sections = []
    for lore_type in _sv()._LORE_TYPE_ORDER:
        chunks = by_type[lore_type]
        if not chunks:
            continue
        budget = _sv()._LORE_TYPE_BUDGETS[lore_type]
        selected, total = [], 0
        for content in chunks:
            if total > 0 and total + len(content) > budget:
                break
            selected.append(content)
            total += len(content)
        if selected:
            header = _sv()._LORE_TYPE_HEADERS[lore_type]
            sections.append(f"[{header}]\n" + "\n".join(selected))

    return "\n\n".join(sections)



def build_events_block():
    """Build the world events/rumors block separately from the stable system prompt.
    Called per-request so NPCs always hear the latest news, but kept outside
    build_system_prompt() so it doesn't break KV cache prefix reuse."""
    settings = _sv().load_settings()
    ge_count = settings.get("global_events_count", 7)
    events_list = []

    # 1. Load Synthesized Rumors (High-level) — cached by file mtime, reloads only when synthesis writes a new rumor
    global _RUMORS_CACHE, _RUMORS_CACHE_MTIME
    world_events_path = os.path.join(_sv().get_campaign_dir(), "world_events.txt")
    if os.path.exists(world_events_path):
        try:
            mtime = os.path.getmtime(world_events_path)
            if mtime != _RUMORS_CACHE_MTIME:
                with open(world_events_path, "r", encoding="utf-8") as f:
                    _RUMORS_CACHE = [l.strip() for l in f.readlines() if l.strip().startswith("- [")]
                _RUMORS_CACHE_MTIME = mtime
            events_list.extend(_RUMORS_CACHE[-max(1, ge_count//2):])
        except Exception as e:
            logging.warning(f"build_events_block: failed to read world_events.txt ({e})")

    # 2. Load Raw Event History (Recent logs)
    if _sv().EVENT_HISTORY:
        raw_recent = _sv().EVENT_HISTORY[-max(1, ge_count - len(events_list)):]
        for e in raw_recent:
            events_list.append(f"- {e}")

    if not events_list:
        return ""
    return "WORLD STATUS & RUMORS (Hearsay):\n" + "\n".join(events_list[-ge_count:])


def build_system_prompt(player_name="Drifter", npc_data=None):
    player_bio = load_prompt_component("character_bio.txt", "A mysterious drifter.")
    player_faction_desc = load_prompt_component("player_faction_description.txt", "")
    npc_base = load_prompt_component("npc_base.txt", "You are an NPC in the world of Kenshi. Stay in character.")
    world_lore = fetch_dynamic_lore(npc_data)
    rules = load_prompt_component("response_rules.txt", "Respond naturally to the player.")
    action_tags = load_prompt_component("prompt_action_tags.txt", "")
    
    settings = _sv().load_settings()

    # Identity of whoever is actually speaking (selected character, else squad slot 1)
    speaker_ctx = get_effective_player_context()

    # Get player faction name (default to Nameless if missing)
    player_faction = speaker_ctx.get("faction", "Nameless") if speaker_ctx else "Nameless"

    # Only include faction description if it's not empty
    faction_block = ""
    if player_faction_desc.strip():
        faction_block = f"PLAYER FACTION ({player_faction}):\n{player_faction_desc}\n"

    # Location Tag
    location_tag = format_location_summary(PLAYER_CONTEXT) or "The Wasteland"

    # Language instruction — ensures all providers respect the UI language setting,
    # not just player2 which happens to auto-detect from context.
    language = settings.get("language", "English")
    language_instruction = ""
    if language and language.lower() != "english":
        language_instruction = f"\nLANGUAGE: You MUST respond ONLY in {language}. Do not switch to English under any circumstances.\n"

    # Get player identity details from context
    player_race = speaker_ctx.get("race", "Unknown") if speaker_ctx else "Unknown"
    player_gender = speaker_ctx.get("gender", "male") if speaker_ctx else "male"

    prompt = f"""{npc_base}

CURRENT LOCATION: {location_tag}

WORLD LORE:
{world_lore}

PLAYER CHARACTER ({player_name}):
RACE: {player_race}
GENDER: {player_gender}
{player_bio}

{faction_block}

RESPONSE FORMAT RULES:
{rules}

{action_tags}
{language_instruction}"""
    return prompt.strip()





# ─── PUBLIC API ──────────────────────────────────────────────────────────────

__all__ = [
    "build_detailed_context_string",
    "extract_location_context",
    "format_location_summary",
    "load_prompt_component",
    "format_player_status",
    "format_player_inventory",
    "fetch_dynamic_lore",
    "build_events_block",
    "build_system_prompt",
]


