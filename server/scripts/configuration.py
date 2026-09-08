import configparser
import logging
import os
import tempfile
import threading

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
KENSHI_SERVER_DIR = os.path.dirname(SCRIPT_DIR)
KENSHI_MOD_DIR = os.path.dirname(KENSHI_SERVER_DIR)
KENSHI_ROOT = os.path.dirname(os.path.dirname(KENSHI_MOD_DIR))

TEMPLATES_DIR = os.path.join(KENSHI_SERVER_DIR, "templates")
CAMPAIGNS_DIR = os.path.join(KENSHI_SERVER_DIR, "campaigns")
CHARACTERS_DIR = os.path.join(KENSHI_SERVER_DIR, "characters")


def resolve_mod_file(filename):
    """
    Helper to find a file in the mod directory.
    Normally files are in KENSHI_MOD_DIR (the root of the mod).
    During development they might be in a 'SentientSands_Mod' subdirectory.
    """
    path = os.path.join(KENSHI_MOD_DIR, filename)
    if os.path.exists(path):
        return path

    dev_path = os.path.join(KENSHI_MOD_DIR, "SentientSands_Mod", filename)
    if os.path.exists(dev_path):
        return dev_path

    alt_path = os.path.join(
        os.path.dirname(KENSHI_MOD_DIR), "SentientSands_Mod", filename
    )
    if os.path.exists(alt_path):
        return alt_path

    return path


INI_PATH = resolve_mod_file("SentientSands_Config.ini")
MODELS_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "models.json")
PROVIDERS_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "providers.json")
NAMES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "names.json")
GENERIC_NAMES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "generic_names.json")
RENAMING_LIST_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "renaming_list.txt")
RENAMING_RULES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "renaming_rules.txt")
LOCALIZATION_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "localization.json")
INTENT_PHRASES_PATH = os.path.join(KENSHI_SERVER_DIR, "config", "intent_phrases.json")

INI_KEY_MAP = {
    "current_model": "CurrentModel",
    "current_campaign": "ActiveCampaign",
    "enable_ambient": "EnableAmbientConversations",
    "radiant_delay": "RadiantDelay",
    "global_events_count": "GlobalEventsCount",
    "synthesis_interval_minutes": "SynthesisIntervalMinutes",
    "favorites": "Favorites",
    "radiant_range": "RadiantRange",
    "talk_radius": "TalkRadius",
    "yell_radius": "YellRadius",
    "min_faction_relation": "MinFactionRelation",
    "max_faction_relation": "MaxFactionRelation",
    "enable_welcome": "EnableWelcomePopup",
    "dialogue_speed_seconds": "DialogueSpeed",
    "bubble_life": "SpeechBubbleLife",
    "language": "Language",
    "chat_hotkey": "ChatHotkey",
    "enable_renamer": "EnableRenamer",
    "enable_animal_renamer": "EnableAnimalRenamer",
    "enable_kayak_knowledge_filters": "EnableKayakKnowledgeFilters",
    "enable_selected_speaker": "EnableSelectedSpeaker",
    # LLM generation parameters
    "profile_max_tokens": "ProfileMaxTokens",
    "profile_temperature": "ProfileTemperature",
    "profile_regen_max_tokens": "ProfileRegenMaxTokens",
    "chat_max_tokens": "ChatMaxTokens",
    "ambient_max_tokens": "AmbientMaxTokens",
    "narrative_max_tokens": "NarrativeMaxTokens",
    "narrative_temperature": "NarrativeTemperature",
    "retry_silent_reply": "RetrySilentReply",
    "llm_timeout_seconds": "LlmTimeoutSeconds",
    # Dialogue history
    "dialogue_history_limit": "DialogueHistoryLimit",
    "prompt_context_limit": "PromptContextLimit",
    "prompt_user_message_buffer": "PromptUserMessageBuffer",
    "native_prompt_history_lines": "NativePromptHistoryLines",
    "profile_regen_history_lines": "ProfileRegenHistoryLines",
    "ambient_speaker_limit": "AmbientSpeakerLimit",
    "ambient_recent_dialogue_per_npc": "AmbientRecentDialoguePerNpc",
    "ambient_local_history_limit": "AmbientLocalHistoryLimit",
    "yell_responder_limit": "YellResponderLimit",
}

_SETTINGS_CACHE = None
_SETTINGS_CACHE_MTIME = 0.0
_INI_WRITE_LOCK = threading.RLock()


def _read_ini(config, path):
    """Read an INI as UTF-8, tolerating a file left over from the old locale writes.

    Everything is written as UTF-8 now. Older installs wrote the INI in the system
    codepage, so a campaign or hotkey with non-ASCII characters would raise
    UnicodeDecodeError here — which used to be swallowed and silently reset every
    setting to its default, including the active campaign.
    """
    try:
        config.read(path, encoding="utf-8")
        return
    except UnicodeDecodeError:
        pass
    try:
        config.read(path)  # legacy locale-encoded file; rewritten as UTF-8 on next save
        logging.warning(f"Settings INI at {path} is not UTF-8; it will be rewritten on the next save.")
    except Exception as exc:
        logging.error(f"Could not read settings INI at {path}: {exc}")


def _atomic_write_ini(config, path):
    """Write the INI through a neighbouring temp file, then rename it into place.

    A direct open(path, "w") leaves a truncated file if the process dies mid-write,
    and _restart_self_async does exactly that: save_settings() followed by
    os._exit(0). A half-written INI means every setting silently reverts to its
    default on the next start, active campaign included.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".ss_ini_", suffix=".tmp", dir=directory)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        config.write(handle)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _save_settings_raw(settings):
    """Save settings to SentientSands_Config.ini."""
    try:
        config = configparser.ConfigParser()
        config.optionxform = str  # preserve PascalCase when writing
        if os.path.exists(INI_PATH):
            _read_ini(config, INI_PATH)

        if "Settings" not in config:
            config["Settings"] = {}

        # Build set of our managed keys (lowercased) so we can remove any
        # old lowercase variants before writing the canonical PascalCase ones.
        managed_lower = {v.lower() for v in INI_KEY_MAP.values()}
        stale_keys = [k for k in config["Settings"] if k.lower() in managed_lower and k not in INI_KEY_MAP.values()]
        for k in stale_keys:
            del config["Settings"][k]

        for key, value in settings.items():
            ini_key = INI_KEY_MAP.get(key)
            if ini_key:
                if isinstance(value, list):
                    config["Settings"][ini_key] = ",".join(value)
                elif isinstance(value, bool):
                    config["Settings"][ini_key] = "1" if value else "0"
                else:
                    config["Settings"][ini_key] = str(value)

        _atomic_write_ini(config, INI_PATH)
    except Exception as exc:
        logging.error(f"Error saving Settings to INI at {INI_PATH}: {exc}")


def load_settings():
    global _SETTINGS_CACHE, _SETTINGS_CACHE_MTIME

    if os.path.exists(INI_PATH):
        try:
            mtime = os.path.getmtime(INI_PATH)
            if _SETTINGS_CACHE is not None and mtime == _SETTINGS_CACHE_MTIME:
                return _SETTINGS_CACHE.copy()
        except Exception:
            pass

    defaults = {
        "current_model": "player2-default",
        "current_campaign": "Default",
        "enable_ambient": True,
        "radiant_delay": 240,
        "global_events_count": 7,
        "synthesis_interval_minutes": 15,
        "favorites": [],
        "radiant_range": 100,
        "talk_radius": 100,
        "yell_radius": 200,
        "min_faction_relation": -100,
        "max_faction_relation": 100,
        "enable_welcome": True,
        "dialogue_speed_seconds": 5,
        "bubble_life": 5.0,
        "language": "English",
        "chat_hotkey": "\\",
        "enable_renamer": True,
        "enable_animal_renamer": True,
        "enable_kayak_knowledge_filters": True,
        "enable_selected_speaker": True,
        # LLM generation parameters
        "profile_max_tokens": 1200,
        "profile_temperature": 0.7,
        "profile_regen_max_tokens": 1500,
        "chat_max_tokens": 500,
        "ambient_max_tokens": 400,
        "narrative_max_tokens": 150,
        "narrative_temperature": 0.8,
        # Переспросить, если модель ответила одними служебными тегами
        "retry_silent_reply": True,
        # Потолок одного обращения к модели. Раньше было 120с и три слепых
        # повтора подряд, то есть до шести минут тишины в игре.
        "llm_timeout_seconds": 60,
        # Dialogue history
        "dialogue_history_limit": 45,
        "prompt_context_limit": 11000,
        "prompt_user_message_buffer": 250,
        "native_prompt_history_lines": 100,
        "profile_regen_history_lines": 80,
        "ambient_speaker_limit": 12,
        "ambient_recent_dialogue_per_npc": 15,
        "ambient_local_history_limit": 80,
        "yell_responder_limit": 6,
    }

    settings = defaults.copy()
    if os.path.exists(INI_PATH):
        try:
            config = configparser.ConfigParser()
            config.optionxform = str  # preserve case on disk
            _read_ini(config, INI_PATH)
            if "Settings" in config:
                # Build case-insensitive lookup so we match regardless of
                # whether the INI was written with PascalCase or lowercase keys.
                section_lower = {k.lower(): v for k, v in config["Settings"].items()}
                for key in defaults.keys():
                    ini_key = INI_KEY_MAP.get(key)
                    if ini_key and ini_key.lower() in section_lower:
                        value = section_lower[ini_key.lower()]
                        if isinstance(defaults[key], bool):
                            settings[key] = value == "1" or value.lower() == "true"
                        elif isinstance(defaults[key], int):
                            try:
                                settings[key] = int(value)
                            except Exception:
                                pass
                        elif isinstance(defaults[key], float):
                            try:
                                settings[key] = float(value)
                            except Exception:
                                pass
                        elif isinstance(defaults[key], list):
                            settings[key] = [
                                item.strip() for item in value.split(",") if item.strip()
                            ]
                        else:
                            settings[key] = value
        except Exception as exc:
            logging.error(f"Error loading settings from INI: {exc}")

    try:
        if os.path.exists(INI_PATH):
            _SETTINGS_CACHE = settings
            _SETTINGS_CACHE_MTIME = os.path.getmtime(INI_PATH)
    except Exception:
        pass

    # Always a copy. The cache-hit branch above already returns one; handing out
    # the cached object here let the first caller mutate global settings for
    # every other thread.
    return settings.copy()


def save_settings(new_settings):
    flat_changes = {}
    for key, value in new_settings.items():
        if key == "radii" and isinstance(value, dict):
            if "radiant" in value:
                flat_changes["radiant_range"] = value["radiant"]
            if "talk" in value:
                flat_changes["talk_radius"] = value["talk"]
            if "yell" in value:
                flat_changes["yell_radius"] = value["yell"]
        else:
            flat_changes[key] = value

    # Read-modify-write must be one step: two threads saving different keys at
    # the same time would otherwise each write a copy built before the other's.
    with _INI_WRITE_LOCK:
        settings = load_settings()
        settings.update(flat_changes)
        _save_settings_raw(settings)


def persist_current_settings():
    """Write the effective settings (with missing defaults filled in) back to the INI file."""
    _save_settings_raw(load_settings())


def get_config_radii():
    settings = load_settings()
    radiant = float(settings.get("radiant_range", 100.0))
    talk = float(settings.get("talk_radius", 100.0))
    yell = float(settings.get("yell_radius", 200.0))
    return radiant, talk, yell
