"""game_locale.py — bridge between Kenshi's own localization and the mod's English data.

Kenshi ships its entire game-data translation in `locale/<lang>/gamedata.po`
(EN original -> localized string). The mod's knowledge base, faction seeding
tables and region maps are all keyed in English, so in a localized game nothing
ever matches: a Russian player's guard reports his faction as "Техохотники",
the lookup for "tech hunters" misses, and every NPC falls back to the same
two-entry default.

This module reads the game's own file and maps localized names back to the
English keys the rest of the mod already speaks.

Ambiguity is refused rather than guessed. Kenshi's role names collapse under
translation — "Бандит" is the localization of Dust Bandit, Hive Bandit, Hungry
bandit and more — so any localized string with several English originals is
returned unchanged instead of resolving to an arbitrary one.

Everything degrades to identity: if the game folder, the locale or the file is
missing, `to_english()` returns its argument and the mod behaves exactly as it
did before.

No Flask, no server imports — pure stdlib.
"""

import logging
import os
import re
import threading

# ---------------------------------------------------------------------------
# Private state
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATE = {
    "loaded": False,
    "language": "",
    "path": "",
    "local_to_english": {},   # normalized localized key -> English original
    "english_to_local": {},   # normalized English key   -> localized string
    "ambiguous": set(),       # normalized localized keys with several originals
}

# "The Hub" and "Hub" must answer to the same key: the lore base spells factions
# and regions both ways ("Holy_Nation" vs "The Holy Nation").
_LEADING_ARTICLE_RE = re.compile(r"^the\s+", re.IGNORECASE)
# Дефис к пробелу, апостроф прочь: игра пишет "Cat-Lon" и "World's End",
# а папки базы — Cat_Lon и Worlds_End.
_WHITESPACE_RE = re.compile(r"[\s_-]+")
_APOSTROPHE_RE = re.compile(r"[’']")

# Russian Kenshi marks gendered nouns inline: "Стражни/кца1/" yields
# "Стражник" and "Стражница". We keep the stem so such names still normalize
# to something stable instead of carrying markup into a lookup key.
_GENDER_MARKUP_RE = re.compile(r"/[^/]*\d*/")


def normalize(name) -> str:
    """Reduce a name to a comparison key: no case, no articles, no underscores."""
    text = str(name or "").strip()
    if not text:
        return ""
    text = _GENDER_MARKUP_RE.sub("", text)
    text = _APOSTROPHE_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _LEADING_ARTICLE_RE.sub("", text)
    return text.casefold()


# ---------------------------------------------------------------------------
# Locating the game
# ---------------------------------------------------------------------------

def _walk_up_to_kenshi_root(start: str) -> str:
    """Find the Kenshi install by walking up from `start`.

    The mod lives at Kenshi/mods/SentientSands, so the root is two levels above
    the mod folder — but the project copy of this repo lives outside any Kenshi
    install, and there the walk simply finds nothing.
    """
    current = os.path.abspath(start)
    for _ in range(8):
        if os.path.isdir(os.path.join(current, "locale")) and \
           os.path.isdir(os.path.join(current, "mods")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def kenshi_root() -> str:
    """Kenshi install directory, or '' when this copy of the mod sits outside one.

    SS_KENSHI_DIR overrides the search — useful for tests and for unusual
    installs where the mod folder was moved.
    """
    override = os.environ.get("SS_KENSHI_DIR", "").strip()
    if override and os.path.isdir(override):
        return override
    return _walk_up_to_kenshi_root(os.path.dirname(os.path.abspath(__file__)))


def _detect_language(root: str) -> str:
    """Active language code from Kenshi's settings.cfg (e.g. 'ru_RU')."""
    path = os.path.join(root, "settings.cfg")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                key, sep, value = line.partition("=")
                if sep and key.strip().lower() == "language":
                    return value.strip()
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PO_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\\"": "\"", "\\\\": "\\"}


def _unescape(text: str) -> str:
    out = text
    for src, dst in _PO_ESCAPES.items():
        out = out.replace(src, dst)
    return out


def _iter_po_entries(path: str):
    """Yield (msgid, msgstr) pairs, joining gettext's multi-line continuations."""
    field = None          # "id" | "str" | None
    msgid, msgstr = [], []

    def _flush():
        if msgid or msgstr:
            return "".join(msgid), "".join(msgstr)
        return None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("msgid "):
                entry = _flush()
                if entry:
                    yield entry
                msgid, msgstr = [_unescape(line[6:].strip().strip('"'))], []
                field = "id"
            elif line.startswith("msgstr "):
                msgstr = [_unescape(line[7:].strip().strip('"'))]
                field = "str"
            elif line.startswith('"') and field:
                chunk = _unescape(line.strip().strip('"'))
                (msgid if field == "id" else msgstr).append(chunk)
            else:
                field = None
    entry = _flush()
    if entry:
        yield entry


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load(force: bool = False) -> bool:
    """Build the name maps. Returns True when a usable translation was found."""
    with _LOCK:
        if _STATE["loaded"] and not force:
            return bool(_STATE["local_to_english"])

        _STATE.update({
            "loaded": True, "language": "", "path": "",
            "local_to_english": {}, "english_to_local": {}, "ambiguous": set(),
        })

        root = kenshi_root()
        if not root:
            logging.info("LOCALE: no Kenshi install found next to this mod copy; "
                         "name translation disabled")
            return False

        language = _detect_language(root)
        _STATE["language"] = language
        if not language or language.lower().startswith("en"):
            logging.info(f"LOCALE: game language '{language or 'unknown'}' needs no "
                         f"translation")
            return False

        path = os.path.join(root, "locale", language, "gamedata.po")
        if not os.path.isfile(path):
            logging.warning(f"LOCALE: {language} selected but {path} is missing; "
                            f"name translation disabled")
            return False
        _STATE["path"] = path

        local_to_english, english_to_local, ambiguous = {}, {}, set()
        try:
            for english, localized in _iter_po_entries(path):
                if not english or not localized:
                    continue
                local_key = normalize(localized)
                english_key = normalize(english)
                if not local_key or not english_key:
                    continue

                english_to_local.setdefault(english_key, localized)

                if local_key == english_key:
                    continue  # untranslated entry carries no information
                previous = local_to_english.get(local_key)
                if previous is None:
                    local_to_english[local_key] = english
                elif normalize(previous) != english_key:
                    # Several English names share this localized string — Kenshi
                    # collapses "Dust Bandit" and "Hive Bandit" into "Бандит".
                    # Guessing here would hand an NPC someone else's lore.
                    ambiguous.add(local_key)
        except OSError as e:
            logging.warning(f"LOCALE: failed to read {path}: {e}")
            return False

        for key in ambiguous:
            local_to_english.pop(key, None)

        _STATE["local_to_english"] = local_to_english
        _STATE["english_to_local"] = english_to_local
        _STATE["ambiguous"] = ambiguous
        logging.info(f"LOCALE: {language}: {len(local_to_english)} names translatable "
                     f"to English, {len(ambiguous)} ambiguous and left alone")
        return bool(local_to_english)


def available() -> bool:
    """True when localized names can be translated to the mod's English keys."""
    load()
    return bool(_STATE["local_to_english"])


def language() -> str:
    load()
    return _STATE["language"]


def stats() -> dict:
    """Snapshot for /lore_debug and tests."""
    load()
    return {
        "language": _STATE["language"],
        "path": _STATE["path"],
        "translatable": len(_STATE["local_to_english"]),
        "ambiguous": len(_STATE["ambiguous"]),
    }


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def to_english(name):
    """Localized game name -> its English original.

    Returns `name` unchanged when it is already English, unknown, or ambiguous.
    """
    text = str(name or "").strip()
    if not text:
        return name
    load()
    return _STATE["local_to_english"].get(normalize(text), text)


def to_english_key(name) -> str:
    """English lookup key for a name in any language ('Святая Нация' -> 'holy nation').

    This is the form the mod's own tables are keyed by: lowercase, no leading
    article, spaces instead of underscores.
    """
    return normalize(to_english(name))


def to_local(name):
    """English name -> the string this game's language shows for it."""
    text = str(name or "").strip()
    if not text:
        return name
    load()
    return _STATE["english_to_local"].get(normalize(text), text)


def is_ambiguous(name) -> bool:
    """True when several English originals share this localized name."""
    load()
    return normalize(name) in _STATE["ambiguous"]
