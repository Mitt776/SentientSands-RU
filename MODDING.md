# Modding SentientSands Kayak

SentientSands Kayak stores most editable world knowledge as plain text. Back up a campaign before changing generated data.

## Main paths

- `Kayak/KayakDB/Template/` — template used to create new campaigns.
- `Kayak/KayakDB/Campaigns/<campaign>/` — campaign-specific knowledge and memory.
- `Kayak/KayakDB/Campaigns/<campaign>/categories/` — factions, locations, NPCs, races, items, and lore.
- `Kayak/KayakDB/Campaigns/<campaign>/mandatory/` — prompt rules and prompt fragments.
- `Kayak/addons/` — optional content packs and manually launched tools.
- `Kayak/bridges/` — integrations with SentientSands and other host systems.

## Documentation

- `Tutorials/KNOWLEDGE_SYSTEM.md` explains entities and indexed knowledge files.
- `Tutorials/prompt_tokens_edit_tutorial.txt` documents prompt tokens and runtime substitutions.
- `Tutorials/commands.txt` documents supported commands and legacy notes.
- `README.md` contains installation, configuration, and the current feature overview.

## Safer editing workflow

1. Copy the campaign or template directory before editing it.
2. Make one small change at a time.
3. Restart Kayak or trigger the relevant reindex operation.
4. Check the Kayak and server logs for parse or index errors.
5. Keep generated personal campaigns out of public repositories unless you deliberately intend to publish them.

Files or folders prefixed with `IGN_` are treated as ignored backups or scratch material where supported.
