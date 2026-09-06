# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**SentientSands with Kayak** (`mod.info`: "Sentient Sands", v0.5.0) — a Kenshi mod that
gives NPCs LLM-driven dialogue, ambient NPC-to-NPC conversation, world narrative
synthesis, and a persistent per-campaign world-memory system ("Kayak"). This repo folder
*is* the deployable mod folder; it is installed to `Kenshi/mods/SentientSands/`.

There is **no build system and no git repository here**. The compiled native plugin
`SentientSands.dll` (loaded by RE_Kenshi via `RE_Kenshi.json`) is a prebuilt binary; its
source is published separately (see `PROJECT_LINKS.md`). Everything you will actually edit
is Python and plain-text data.

## Architecture

Two cooperating Flask HTTP servers on localhost, plus an optional third:

| Service | Port | Entry point | Role |
|---|---|---|---|
| SentientSands server | 5000 | `server/scripts/kenshi_llm_server.py` | Talks to the game (via the DLL), owns identity/context resolution, calls the LLM, decides *what* to persist |
| Kayak | 5001 | `Kayak/kayak_server.py` | Text-file knowledge DB: indexing, retrieval, prompt assembly, per-campaign persistence |
| SentientSongs | 4999 | `Kayak/bridges/SentientSongs/SentientSongs.py --serve` | Optional local music player controlled by in-game `/m_*` commands |

**Startup flow:** the DLL launches the SentientSands server with the game. The SS server
auto-starts Kayak as a subprocess (`_kayak_try_connect` in `kenshi_llm_server.py`) and
stops it on exit only if it spawned it. If Kayak is unreachable, the SS server falls back
to "native" prompts built without the knowledge engine.

**The bridge boundary** (`Kayak/bridges/sentient_sands/`) is the only coupling between the
two servers and is deliberately layered:
- `hub.py` — `KayakHub`: thin HTTP client, retries, circuit breaker. No business logic.
- `bridge.py` — `SentientSandsBridge`: all SS-specific decisions (faction/race knowledge
  seeding tables, action-hint detection, relation→weight, pricing, field-name translation
  SS→Kayak). Decides *what* to read/write; delegates transport to `hub.py`.
- `server/scripts/ss_character_gateway.py` — `CharacterGateway`: the single canonical
  read/write path for character data on the SS side. Merge-mode writes; manual edits to
  `entity.txt` survive because unrecognized fields are preserved.

### SentientSands server modules (`server/scripts/`)

`kenshi_llm_server.py` is ~8.8k lines and holds all Flask routes. It delegates to:
- `configuration.py` — path resolution + `SentientSands_Config.ini` load/save. Paths are
  derived by walking up from `server/scripts/`: `KENSHI_MOD_DIR` = this repo root.
- `server_runtime.py` — shared mutable runtime state (`runtime` SimpleNamespace).
- `ss_identity.py` — live NPC context resolution, target disambiguation, ambient speaker
  selection, direct-chat leasing.
- `ss_prompt.py` / `ss_llm.py` — native (non-Kayak) prompt assembly; LLM call + JSON repair.
- `ss_persistence.py` / `ss_character_registry.py` / `ss_character_creation_handler.py` —
  storage IDs, name registry, profile upgrade decisions, batch profile generation.
- `personality_rules.py` — trait/speech generation, `ANIMAL_RACES`/`MACHINE_RACES`,
  reads `config/persona_races.json`.
- `campaign_chronicle.py` — persistent major-event history + `[CAMPAIGN CHRONICLE]` block.
- `save_reader.py` — binary Kenshi save parsing for name/world indexing.
- `simplify_global_events.py` — event-log cleanup/dedup before synthesis.

### Kayak modules (`Kayak/scripts/`)

- `campaign_manager.py` — `CampaignManager`: create campaign = `cp -r KayakDB/Template
  KayakDB/Campaigns/<name>`; load/switch = full reindex. **`Template/` is never read at
  runtime**, only copied.
- `indexer.py` — builds the in-memory index from `entity.txt`, `who_knows_me.txt`,
  `define_children.txt`. `$`-prefixed fields = prose (not link seeds, not keyword-indexed
  by default).
- `retriever.py` — layered keyword→entity expansion; `non_expand_fields` config +
  `_is_text_field()` prevent prose from triggering expansion.
- `prompt_builder.py` — executes a `PromptPolicy` set by the bridge; assembly order is
  mandatory files → WORLD_CONTEXT → TARGET_NPC → DIALOGUE → PLAYER_MESSAGE.
- `token_resolver.py` — expands whitelisted `<...>` tokens in user-authored prompt files
  (the newer explicit path; must **not** call `Retriever.retrieve()`).

### Data model

Every entity is a folder with `entity.txt` (`key = value` lines; `$key` = prose-only).
Aux files: `dialogue.txt`, `stats.txt`, `notes.txt`. `IGN_`-prefixed files/folders are
ignored by Kayak. **Reindex rule:** changes to `entity.txt`, `who_knows_me.txt`,
`define_children.txt` need a reindex; `stats.txt` / `dialogue.txt` are read fresh at
prompt time. Mandatory prompt files load only if numerically prefixed (`1_`, `2_`…);
`ph_` = placeholder, not loaded.

Campaign data lives in `Kayak/KayakDB/Campaigns/<campaign>/`. The SS server keeps a
parallel `server/campaigns/<campaign>/` for its own runtime state. Campaigns from before
v0.4 are not format-compatible.

## Common commands

Bundled runtime is embedded CPython 3.13.2 at `server/python/python.exe`. Prefer it; the
`.bat` files fall back to `py -3` when it is absent.

```sh
# Install dependencies (flask, flask-cors, requests, pygame)
INSTALL_DEPENDENCIES.bat            # or: server/python/python.exe INSTALL_DEPENDENCIES.py

# (Re)create the embedded Python runtime from scratch
server/python/python.exe server/setup_embedded_python.py   # downloads python-3.13.2-embed

# Run servers manually (normally the DLL + auto-start handle this)
server/python/python.exe server/scripts/kenshi_llm_server.py     # port 5000
Kayak/START_KAYAK.bat                                            # port 5001
Kayak/START_SENTIENTSONGS_SERVICE.bat                            # port 4999

# Push config_master.txt -> the three real config files
python apply_config.py [--dry-run]

# Prepare a clean release/dev copy (interactive: strips campaigns, resets API keys)
LAUNCH_CLEANER.bat

# GUI log/prompt inspector (Tkinter, talks to port 5000)
server/python/python.exe server/scripts/visual_debugger.py
```

### Tests

There is no test framework. `server/scripts/test_recruit_fix.py` is a manual integration
script that POSTs to a **running** server on `:5000` (`SS_TEST_CHAT_URL`, `SS_TEST_CHAR_DIR`
override the targets). Run it only against a live dev server.

Useful live endpoints while debugging: `GET :5001/status`, `POST :5001/campaign/reload_index`,
`POST :5001/config/reload`, `curl :5000/lore_debug`.

## Configuration

- **`config_master.txt`** is the single edit point. `apply_config.py` fans it out to
  `SentientSands_Config.ini` (`[SentientSands]` → `[Settings]`, PascalCase keys via
  `INI_KEY_MAP` in `configuration.py`), `Kayak/config/core_config.txt` (`[Kayak]`), and
  `server/config/renaming_rules.txt` (`[Renaming]`). It only updates keys it names and
  preserves comments. Restart servers after applying.
- **LLM providers/models:** `server/config/providers.json` (API keys, base URLs) and
  `server/config/models.json` (model key → provider+model). These ship with placeholder
  keys; the in-game F8 menu selects the active model. `LAUNCH_CLEANER.py` holds the
  canonical `DEFAULT_PROVIDERS` / `DEFAULT_MODELS` used when resetting.
- Ports 4999/5000/5001 are hardcoded in several places; changing them is not a one-liner.

## Conventions & constraints

- **License:** project code is GPL-3.0(-or-later where a file says so). Pineaxe-authored
  Kayak files carry a GPLv3 §7(b) attribution header pointing at `Kayak/ADDITIONAL_TERMS.md`
  and `CREDITS.md` — keep those headers when editing those files. Credit line:
  "SentientSands Kayak by Harvicus and Pineaxe."
- The v0.5.0 changelog states the DLL was **not recompiled** — only two NUL-terminated UI
  strings were patched in place and the PE checksum recalculated. Do not assume DLL
  behavior can be changed from this repo.
- Entity/campaign names are validated against path traversal (`_safe_segment`,
  `_validate_campaign_name`) — keep that guard on any new file-writing endpoint.
- Match surrounding style: stdlib-only where a module's docstring says so
  (`campaign_chronicle.py`, `dack.py`), `# ─── SECTION ───` comment banners in Kayak code.
- Windows is the primary platform; `.bat` launchers and `os.startfile` are load-bearing.
- Modding/authoring docs live in `MODDING.md`, `Tutorials/KNOWLEDGE_SYSTEM.md`
  (`who_knows_me.txt` / `define_children.txt` semantics), and
  `Tutorials/prompt_tokens_edit_tutorial.txt` (the `<...>` token catalog).
