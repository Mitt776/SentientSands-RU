# SentientSands Kayak v0.5.0
This README covers installation, feature overview, prompt behavior, database structure, and the main modding entry points for the bundled release.

## License and legal notes

Project-authored SentientSands and Kayak code is free software distributed under the GNU General Public License, version 3. Individual files may expressly permit later GPL versions. See `LICENSE`.

Kayak material for which Pineaxe holds copyright is also subject to the attribution condition permitted by GPLv3 section 7(b) and documented in `Kayak/ADDITIONAL_TERMS.md`. This is not a separate proprietary license and does not remove the freedoms granted by the GPL.

The corresponding source code for this release is distributed separately through the official source repository listed in `PROJECT_LINKS.md`.

The bundled `server/python/` runtime contains third-party components under their own licenses. See `THIRD_PARTY_NOTICES.md`. RE_Kenshi and KenshiLib are external dependencies and are not redistributed by this project.

Any LLM provider you configure may receive dialogue content and game-derived context while the mod is in use. Review the privacy policy for the provider you choose.

## Attribution

**Primary developers:** Harvicus and Pineaxe.

Required Kayak attribution for downstream distributions covered by the additional terms:

> SentientSands Kayak by Harvicus and Pineaxe.

The maintained Discord, Nexus, Steam, and source-repository links are listed in `PROJECT_LINKS.md`.

## Overview

SentientSands's Kayak provides:

- text-based world knowledge storage in `KayakDB`
- layered retrieval across factions, locations, NPCs, items, races, and lore
- campaign-isolated data so each playthrough stays separate
- prompt assembly for chat, biography, loremaster, and speak systems
- dialogue and runtime stats storage per entity
- NPC knowledge growth and faction-based knowledge seeding
- economy and trader prompt support

## Installation

### Folder Placement

The `SentientSands` folder placement is Kenshi/Mods/SentientSands .

### Dependency Setup

EXTERNAL DEPENDENCIES:
KenshiRE and KenshiLib must be installed in your Kenshi game, follow their install instructions.

KenshiRE:
https://www.nexusmods.com/kenshi/mods/847

The most updated version of KenshiRE is bundled with it's version of KenshiLib.

The normal dependency step is:

SentientSands/
INSTALL_DEPENDENCIES.bat

If you need to run the installer manually (some Linux and VM users may have to):

py -3 INSTALL_DEPENDENCIES.py

### Configure Your AI Provider

If the installer doesn't open the files automatically before shutting down, edit these files in `server\config\`:

- `providers.json`
- `models.json`

Replace only the placeholder API key for the provider you plan to use. Local providers such as Ollama can keep their local placeholder key.

Example `providers.json`:

```json
{
  "openrouter": {
    "api_key": "YOUR_OPENROUTER_KEY",
    "base_url": "https://openrouter.ai/api/v1"
  },
  "ollama": {
    "api_key": "ollama",
    "base_url": "http://localhost:11434/v1"
  }
}
```

Example `models.json`:

```json
{
  "kimi-k2.5": {
    "provider": "openrouter",
    "model": "moonshotai/kimi-k2.5"
  },
  "ollama-llama3": {
    "provider": "ollama",
    "model": "llama3"
  }
}
```

Model selection happens in the Sentient Sands in-game settings window. (Press F8 in-game).


### Startup

SentientSands server launches with the game. In a normal install.

SentientSands uses port 5000, and Kayak uses port 5001, the music system SentientSongs uses port 4999. Make sure no other process is blocking them.

### Existing Saves

Campaigns created before v0.4 are not compatible with the current format. To recover lore from an older campaign, create a new campaign on the same save, talk to the NPCs you care about, wait for their profiles and first responses to be generated, and then copy only the useful text into the matching NPC folders under `KayakDB/Campaigns/<your campaign>/categories/base_npcs/`. Back up both campaigns first.

### In-game instructions:

Once you have loaded your save game with SentientSands for the first time, you'll need to press F8 to access the in-game menu for SentientSands. Open Settings to select your provider and model, save. Open Settings again and click on TEST. If you get LLM Okay, you're good to go. 

Then, on the same F8 menu, open Campaigns and create your new campaign. 

Use your selected hotkey from settings in-game by clicking on an NPC and pressing it, a chat window will open. The character who speaks is always the character who occupies position 1 of your squad 1.

## Troubleshooting

### SentientSands server and/or Kayak will not start

- make sure dependencies were installed
- run `Kayak\START_KAYAK.bat` manually if you need to see the startup error.
- make sure ports 4999, 5000 and 5001 are available and no other programs are using them.

### `pip is not recognized`

Python is not available in PATH. Install Python and retry the setup scripts.

### NPCs feel different or worse than expected

Kayak may not be running. Check whether anything is listening on `127.0.0.1:5001` or inspect the SentientSands server log and Kayak's log.

## SentientSands's Kayak Feature List

### Context Engine

- text-based database with no binary formats, SQL server, or external database layer
- layered retrieval from keyword hits to linked entities
- configurable limits such as `max_keywords`, `max_layers`, `max_matches_per_layer`, `max_files`, and `timeout_ms` (not needed when using the specific prompt tokens - check 'prompt_tokens_edit_tutorial.txt)
- authored knowledge access with `who_knows_me.txt` and authored expansion with `define_children.txt`
- weight-based priority when context space is limited

### Entity System

- folder-per-entity layout
- free-form `key = value` fields
- structural fields for indexing and cross-entity linking
- prose fields with `$` prefix for prompt-only text
- auxiliary files such as `dialogue.txt`, `stats.txt`, and `notes.txt`
- `IGN_` prefix support for ignored backups, archives, and scratch files, prevents Kayak from reading them.

### Campaign System

- separate data per campaign
- template-based campaign creation from `KayakDB\Template`
- hot campaign switching and index reloads
- campaign-specific NPC persistence in `campaign_npcs`

### Prompt Building

- mandatory prompt files loaded in numeric order
- separate prompt types for Chat, Species override, Biography, Loremaster, and Speak
- stats injection from runtime `stats.txt`
- per-NPC's Dialogue injection from dialogue.txt

### Economy And Trader Support

- layered global, city, and NPC price modifiers (experimental and unreliable in most cases).
- in-game commands for adjusting price behavior
- trader inventory injection when the upstream context marks an NPC as a trader (requires prompt token <target_npc_context>)

### Mandatory Prompt Folders

Each campaign has mandatory prompt folders in:

```text
KayakDB\Campaigns\<campaign>\mandatory\
```

- `Chat/`
- `Biography/`
- `Loremaster/`
- `Speak/`
etc.

Only files with numeric prefixes such as `1_`, `2_`, `3_` are auto-loaded. 

Placeholder files starting with `ph_` are not loaded.

## Entity Format

Every entity is a folder with an `entity.txt` file.

Example:

```text
Category = base_npcs
Name = Holy_Phoenix
Id =

display_name = Holy Phoenix
faction = Holy_Nation
race = Greenlander
weight = 9

$personality = A terrible and serene god-king who rules by divine mandate.
$backstory = He claims to be the reincarnation of the first Phoenix...
```

### Structural Fields

Fields without `$` are used for indexing and link expansion. If a value matches another entity name, Kayak can pull that entity as related context.

### Prose Fields

Fields beginning with `$` are injected as text but not used as link seeds. Use them for personality, background, lore text, and descriptions.

### Auxiliary Files

Common auxiliary files in an entity folder:

- `dialogue.txt`
- `stats.txt`
- `notes.txt`
- `IGN_*` files and folders for ignored backups and archives

## Runtime Data And Logs

- Kayak runs on `127.0.0.1:5001`
- campaign data lives in `KayakDB\Campaigns\<campaign>`
- prompt and retrieval logs live in `KayakDB\Campaigns\<campaign>\logs`
- reindex manually with `POST /campaign/reload_index` or by restarting the server

## Modding

For adding custom NPCs, locations, factions, items, lore, and prompt content, use `MODDING.md`.

That file is the main long-form guide for creating Kayak-compatible content packs and integrations.

## Addons And Legacy Import Tools

The `addons/` directory is for optional tools and content packages that do not belong in `bridges/`.

Typical uses:

- content packs that add lore, NPC, faction, or item files
- maintenance tools that migrate, rename, or audit campaign data
- server extensions or one-off utilities launched manually

Bridges for specific host mods should stay under `bridges/<mod_name>/`, not under `addons/`.

Addons are not auto-run. Launch them manually, or from your own setup flow. Back up your campaign before running anything that edits data.

## Credits

**Primary developers:** Harvicus and Pineaxe.

**Contributors:** Wirlocke and ConcreteFoundry.

**Special thanks:** BFrizzleFoShizzle and the RE_Kenshi/KenshiLib contributors.

See `CREDITS.md` for the maintained credit list.
