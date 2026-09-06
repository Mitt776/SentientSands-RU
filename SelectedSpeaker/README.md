# SelectedSpeaker

An RE_Kenshi plugin that makes SentientSands talk as the character you have
**selected in-game**, instead of always squad 1 slot 1.

## Why a separate plugin

The stock `SentientSands.dll` serialises `playerCharacters[0]` as the speaker.
No source for the shipped v0.5.0 DLL is published, so this plugin sits alongside
it rather than replacing it: it reads the live selection and reports it to the
same Python server on `POST /selected_character`. The server layers that record
over the stock one for as long as the reports keep arriving.

Nothing in the `SentientSands` mod folder is modified. `SHA256SUMS.txt` stays
valid, and updating SentientSands cannot silently undo the change.

## Behaviour

- Polls `ou->player->selectedCharacter` roughly twice a second, inside a hook on
  `PlayerInterface::update`.
- **Latches** the last selection that was one of your own characters. Clicking an
  NPC — which is exactly how the SentientSands chat window is opened — or
  clearing the selection does not change who is speaking.
- Sends only per-character fields (name, ids, race, gender, faction, job, state,
  money, stats, medical, inventory). World state stays with the stock DLL, so
  time, weather, events and nearby NPCs cannot drift between the two senders.
- Sends `{"clear": true}` when no player character is latched, so the server
  reverts to stock behaviour immediately.
- If the plugin is absent, disabled, or the server is unreachable, the server's
  10-second freshness window lapses and the mod behaves exactly as before.

The server side can also be switched off without touching the plugin:
`EnableSelectedSpeaker = 0` in `SentientSands_Config.ini` (or in
`config_master.txt`, then run `apply_config.py`). It is re-read live — no restart.

## Build requirements

KenshiLib plugins **must** be compiled with the Visual C++ 2010 x64 compiler to
match Kenshi's ABI. This is not optional and is the main setup hurdle.

- Visual Studio 2019 or newer, **plus** the VC++ 2010 (v100) x64 toolset
- [KenshiLib](https://github.com/BFrizzleFoShizzle/KenshiLib) and its
  [dependencies](https://github.com/BFrizzleFoShizzle/KenshiLib_Examples_deps)
- Boost (KenshiLib is built against 1.60.0)
- **Release only.** The Debug configuration is broken upstream, so this project
  does not define one.

The project reads three environment variables, the same ones the official
examples use:

| Variable | Points at |
|---|---|
| `KENSHILIB_DIR` | KenshiLib checkout (provides `Include/` and `Libraries/`) |
| `KENSHILIB_DEPS_DIR` | the `KenshiLib_Examples_deps` checkout |
| `BOOST_INCLUDE_PATH` | Boost headers |

Then build `SelectedSpeaker.vcxproj` as `Release|x64`.

## Install

1. Install [RE_Kenshi](https://www.nexusmods.com/kenshi/mods/847) — it bundles
   the matching KenshiLib.
2. Copy `dist/` to `<Kenshi>/mods/SelectedSpeaker/`.
3. Copy the built `SelectedSpeaker.dll` into that same folder.
4. Enable **SelectedSpeaker** in Kenshi's `Mods` tab, alongside SentientSands.

Final layout:

```
<Kenshi>/mods/SelectedSpeaker/
    SelectedSpeaker.dll
    SelectedSpeaker.mod
    RE_Kenshi.json
```

`SelectedSpeaker.mod` is a 46-byte empty-mod stub containing no name or data —
it exists only so Kenshi lists the mod. It is byte-identical to the stub the
official KillButton example ships.

## Verifying it works

With the game running and a save loaded:

```sh
curl http://127.0.0.1:5000/context
```

`selected.fresh` should be `true`, `selected.context.name` should be the
character you have selected, and `effective_player` should show that character's
race, health and inventory while `player` still shows squad 1 slot 1. Select a
different squad member and the values follow; click an NPC and they must not.

## Notes

- The serialiser mirrors `GetDetailedContext()` in the public SentientSands
  source (`src/Context.cpp`, GPL-3.0). Field names and the hunger convention
  match it deliberately — the server parses both with the same code.
- Inventory is walked on Kenshi's main thread only, which is why serialisation
  happens inside the update hook. The HTTP POST is handed to a worker thread so
  a stalled server can never block the game loop.
- Multi-selection uses Kenshi's primary `selectedCharacter`, not the
  `selectedCharacters` set; box-selecting a squad reports whichever character
  the engine considers primary.

## License

GPL-3.0-or-later, as with the rest of the project. See `../LICENSE`.
