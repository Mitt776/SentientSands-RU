# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
kayak_keys.py — Data schema reference for Kayak bridge builders.

This is the "master key" that describes:
  1. What data categories exist in Kayak
  2. What fields each category has
  3. How to read and write each type
  4. What constraints and conventions apply

ANY mod can use this as a template to build a bridge to Kayak.
The SentientSands bridge is an example implementation.

DESIGN PRINCIPLE:
  - Bridges are decision points (what data to gather, how to transform)
  - Core is execution engine (indexing, retrieval, assembly)
  - Keys are the contract between them
"""

# ─── DATA CATEGORIES ──────────────────────────────────────────────────────────

"""
Every entity in Kayak belongs to a category. Categories are discovered at runtime
from the folder structure: KayakDB/Campaigns/<campaign>/categories/<Category>/<Name>/

Common categories (examples; not exhaustive):
  - campaign_npcs       Master NPC profiles
  - world_locations     Cities, regions, landmarks
  - world_events        Rumors, major happenings, divine acts
  - world_factions      Faction descriptions, relationships
  - world_items         Item definitions, base prices
  - world_lore          Lore chunks, history, mythology
  - daily_events        Time-scoped occurrences (one NPC, one day)
  - rumors              Spreading word, gossip chains
"""

# ─── ENTITY STRUCTURE ──────────────────────────────────────────────────────────

"""
Every entity.txt file has three parts:

1. HEADER (required, three lines):
   Category = <name>
   Name = <folder_name>
   Id = <persistent_id_or_empty>

2. FIELDS (key = value, ~key: value, key -> value, etc.):
   Fields starting with $ are prose/text (indexed for search, never expanded)
   All other fields are candidates for layer-based expansion
   
   Field naming:
     - Lowercase with underscores (normalized on read)
     - No spaces in keys
     - Values support comma-separated lists for references

3. FREE TEXT (prose at end):
   Any unstructured lines at end of file
   Useful for narrative, background, context

Example entity.txt:
   Category = campaign_npcs
   Name = Bob_Miller
   Id = 1-304443360-1-2050292992-1

   display_name = Bob Miller
   race = Human
   faction = Holy_Nation
   loyalty = Very_High
   $personality = Disciplined, severe, speaks little
   $backstory = Born in Blister Hill...
   $knows_about = Blister_Hill, Holy_Lands, Stack
   
   Additional context:
   Bob is a veteran soldier...
"""

# ─── KEY CONVENTIONS ──────────────────────────────────────────────────────────

"""
**FIELD PREFIXES (Structural Hints)**

  $field_name    Prose/text field
                 - Indexed for keyword search
                 - NEVER used for layer expansion
                 - Examples: $personality, $backstory, $speech_quirks
                 - Rendered without $ prefix to LLM

  No prefix      Reference/structural field
                 - Used for layer expansion if value matches another entity name
                 - Examples: faction, origin_faction, location, led_by, knows_about
                 - Candidates for retrieval-time expansion

**IDENTITY FIELDS (Canonicalization)**

  persistent_id  Stable Kenshi UUID (if available)
                 Example: 1-304443360-1-2050292992-1
                 Persists across save/load cycles

  runtime_id     Session-scoped ID (changes per game boot)
                 Used when persistent_id unavailable
                 Falls back to generic name-based ID

  Id (header)    Most stable ID available
                 Resolved at write-time: persistent > runtime > generated

**RETRIEVAL CONTROL FIELDS**

  weight         Float (default 5.0)
                 Affects scoring during retrieval
                 Higher = more likely to appear in context
                 Example: weight = 7.5 (important NPC)

  non_expand_fields (in config, not per-entity)
                 Frozenset of field names that should NEVER trigger expansion
                 Structural fields: id, weight, display_name, etc.
"""

# ─── READING DATA ─────────────────────────────────────────────────────────────

"""
**ENTITY RETRIEVAL FLOW**

1. Bridge provides keywords + optional target entities
2. Retriever.retrieve(keywords) → List[Entity]
   - Layer 0: keyword/name matches
   - Layer N: expand via field references
   - Stops at: max_layers, max_files, or timeout
3. Bridge gets back Entity objects with:
   - uid, category, name, entity_id, persistent_id, runtime_id
   - fields (dict), free_text (str), path, weight
4. Bridge can load auxiliary files:
   - stats.txt (fresh read at prompt-time)
   - dialogue.txt (most recent lines)
   - Any .txt in the entity folder

**AUXILIARY FILES (Per-Entity)**

  stats.txt       Live state (blood %, location, etc.)
                  Read fresh at prompt-time, NOT indexed
                  Updated frequently: health, position, inventory

  dialogue.txt    Conversation history with timestamps
                  Most recent N lines kept (older lines archived)
                  Format: "Player: ...\nNPC: ...\n"
                  Trimmed to keep_lines (configurable, default 30)

  price.txt       Item pricing overrides (if entity is an item)
                  Natural key: base_price_cats = <number>
                  Read only when displaying item context

  Other files     Custom use-case specific
                  Accessed via prompt_builder.render_entity(aux_files=[...])
"""

# ─── WRITING DATA ─────────────────────────────────────────────────────────────

"""
**ENTITY WRITES (with reindex)**

Trigger: Writing entity.txt
Effect: Incremental reindex of that entity (add_or_update_entity)
API: POST /write/npc

Bridge responsibility:
  - Build entity.txt content with proper structure
  - Decide which fields to include
  - Preserve existing fields not being updated (merge strategy)
  - Call after any profile-level change

Example write:
  {
    "category": "campaign_npcs",
    "name": "Bob_Miller",
    "entity_content": "Category = campaign_npcs\nName = Bob_Miller\n..."
  }

**AUXILIARY WRITES (no reindex)**

Trigger: Writing stats.txt, dialogue.txt, etc.
Effect: No reindex; read fresh at prompt-time
API: POST /write/stats, POST /dialogue/save, etc.

Bridge responsibility:
  - Write fresh state to stats.txt (no reindex needed)
  - Append dialogue exchanges (trimmed to keep_lines)
  - Archive overflow to IGN_dialogue_backup/

Example stats write:
  {
    "target_npc": "Bob_Miller",
    "stats_content": "blood: 75%\nlocation: Blister Hill\n..."
  }

**KNOWLEDGE GROWTH**

Mechanism: NPC learns about locations as they experience them
Effect: Adds location names to their $knows_about field
API: POST /write/npc_knowledge

Bridge responsibility:
  - Track locations NPC has been in dialogue
  - Call grow_npc_knowledge() after each conversation
  - Validates location against index before writing
"""

# ─── PROMPT BUILDING ──────────────────────────────────────────────────────────

"""
**PROMPT POLICY**

Bridge supplies a policy that tells the core what to include:

  prompt_type           : str  (Chat, Loremaster, Biography, Speak, etc.)
  target_aux_files      : List[str]  (e.g., ["stats.txt"])
  include_dialogue      : bool
  dialogue_keep_lines   : int
  world_aux_files       : List[str]  (usually [])
  player_message_suffix : str  (mode instructions, e.g., "WHISPER mode...")
  target_label          : str  (section name in prompt)

The core executes the policy WITHOUT making any decisions:
  1. Load mandatory blocks (numbered files from mandatory/<PromptType>/)
  2. Retrieve world context via keywords + optional priority entities
  3. Filter world context if knowledge_filter provided
  4. Render target NPC + aux files
  5. Include dialogue if policy says so
  6. Append player message + suffix

**MANDATORY BLOCKS**

Located: Campaigns/<campaign>/mandatory/<PromptType>/
Files: Numbered (e.g., 1_system_rules.txt, 2_tone.txt, 3_action_tags.txt)
Order: Numeric, loaded in sequence
Scope: Shared across all prompts of same type (Chat, Loremaster, etc.)

This is where structural rules live (can't change without full reindex).
Use /write/mandatory to update per-campaign (e.g., player bio).

**KNOWLEDGE FILTER**

Optional list of entity names the target NPC is allowed to know about.
If provided: world context filtered to only these entities.
If omitted (None): all retrieved entities included.

Bridge responsibility:
  - Read NPC's $knows_about field
  - Parse into list of entity names
  - Pass to retriever as knowledge_filter

This allows intelligent world context scoping:
  - New NPC → sees everything (all possible world context)
  - Experienced NPC → sees only what they've learned
"""

# ─── CONFIGURATION CONTROL ──────────────────────────────────────────────────────

"""
**RETRIEVAL TUNING (core_config.txt)**

  max_keywords       : int  (default 5)
                       How many keywords to extract from input

  max_layers         : int  (default 3)
                       Maximum expansion depth
                       Layer 0 = direct, Layer 1+ = via field expansion

  max_matches_per_layer : int  (default 5)
                       How many new candidates each entity can queue

  max_files          : int  (default 10)
                       Hard limit on returned entities

  timeout_ms         : int  (default 1500)
                       Deadline for retrieval (milliseconds)

  dialogue_keep_lines : int  (default 30)
                       How many dialogue lines to retain per NPC

**ECONOMY TUNING**

  price_modifier     : float  (default 1.0)
                       Global economy multiplier
                       Set via POST /config/set
                       Persists to core_config.txt
                       Applies to all shopkeepers instantly

Bridge responsibility:
  - Read current configs via POST /config/get
  - Adjust retrieval parameters for fast/slow contexts
  - Set global or city/NPC-specific price modifiers
  - No hardcoding — all tuning via config
"""

# ─── HTTP API (Kayak Server) ──────────────────────────────────────────────────

"""
Bridge talks to Kayak via HTTP. Key endpoints:

**CAMPAIGN**
  POST /campaign/switch {name}  — Create-if-needed + load (main save hook)

**WRITING**
  POST /write/npc               — Write entity.txt (reindexes)
  POST /write/stats             — Write stats.txt (no reindex)
  POST /write/mandatory         — Sync mandatory files per campaign
  POST /write/world_event       — Create world event entity
  POST /write/npc_knowledge     — Add location to NPC's knows_about

**DIALOGUE**
  POST /dialogue/save           — Append player+NPC exchange
  POST /dialogue/read           — Get conversation history

**PROMPT BUILDING**
  POST /prompt/chat             — Build chat prompt
  POST /prompt/loremaster       — Build loremaster prompt (world narrative)
  POST /prompt/biography        — Build biography prompt (NPC generation)
  POST /prompt/speak            — Build echo prompt (no context)

**ENTITY QUERY**
  POST /entity/fields           — Read an entity's fields dict

**CONFIGURATION**
  POST /config/set {key, value} — Set + persist config
  POST /config/get              — Read current config

**STATUS**
  GET /status                   — Health check + index stats

All endpoints return JSON: {status, data/error, warnings, context, ...}
"""

# ─── BRIDGE PATTERN ──────────────────────────────────────────────────────────

"""
A Kayak BRIDGE is responsible for:

1. TRANSLATION
   - Convert mod's internal data shapes to Kayak entity structure
   - Normalize field names (mod_field → kayak_field)
   - Validate required fields

2. ROUTING
   - Decide what data to write and when
   - Decide what data to retrieve and how to filter it
   - Manage cache vs fresh reads

3. BUSINESS LOGIC
   - Faction-based knowledge seeding
   - Relation-based entity weighting
   - Action hint detection and injection
   - Pricing calculation (global × city × NPC modifiers)

4. ERROR HANDLING
   - Validate before POSTing
   - Graceful fallback if Kayak unavailable
   - Log errors for debugging

Example bridges:
  - SentientSands bridge (convert SS fields to Kayak entities)
  - A dungeon mod bridge (convert dungeon state to events)
  - A faction mod bridge (sync faction relationships)
  - A weather mod bridge (inject weather as world events)

Each bridge owns its translation layer and business logic.
Code is separate, configuration is shared, data format is standard.
"""

# ─── SUMMARY ──────────────────────────────────────────────────────────────────

"""
To build a bridge:

1. Understand your mod's data model
2. Map it to Kayak categories and fields
3. Follow naming conventions ($ for prose, others for expansion)
4. Implement write path: entity → POST /write/npc
5. Implement read path: keywords → POST /prompt/chat
6. Handle knowledge filtering, pricing, events as needed
7. Test without breaking existing bridges (Kayak is shared)

Key principle: Bridges are DECISION POINTS, not APIs.
The core doesn't know about your mod. Your bridge decides
what data matters, how to shape it, and what to do with results.
"""
