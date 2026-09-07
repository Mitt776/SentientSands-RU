"""campaign_chronicle.py — Campaign chronicle storage and prompt injection.

Manages persistent major-event history for a campaign:
  - load_chronicle / save_chronicle: JSON read/write with archive overflow
  - append_major_event: atomic load → append → save (call this from routes)
  - build_chronicle_block: formats the [CAMPAIGN CHRONICLE] prompt section

No Flask or server imports — pure stdlib only.
"""

import json
import logging
import os
import re
import time

try:
    # Карта имён из локализации самой игры. Лежит рядом; при её отсутствии
    # сравнение имён остаётся простым — как и было.
    import game_locale as _game_locale
except Exception:  # pragma: no cover
    _game_locale = None

# ---------------------------------------------------------------------------
# Private constants (only used within this module)
# ---------------------------------------------------------------------------

_CHRONICLE_MAX_ACTIVE = 15

# Maps region name → set of faction names geographically present there.
_KENSHI_REGION_FACTIONS = {
    "Holy Lands":      {"Holy Nation", "Holy Nation Outlaws", "Flotsam Ninjas",
                        "Hiningenteki Hantas", "Highlanders"},
    "Great Desert":    {"United Cities", "Traders Guild", "Slave Traders",
                        "Anti-Slavers", "Tech Hunters", "Western Hive"},
    "South Wetlands":  {"United Cities", "Traders Guild", "Slave Traders", "Anti-Slavers"},
    "Stenn Desert":    {"Shek Kingdom", "Kral's Chosen", "Band of Bones", "Berserkers", "Reavers"},
    "Hook":            {"Shek Kingdom", "Reavers", "Band of Bones"},
    "Black Desert":    {"Mechanical Hive", "Second Empire Exile", "Tech Hunters", "Skeletons"},
    "Iron Valleys":    {"Mechanical Hive", "Tech Hunters"},
    "Grey Desert":     {"Mechanical Hive", "Skeletons", "Tech Hunters"},
    "The Swamp":       {"Blue Cleavers", "Green Katanas", "Swamp Ruffians", "Cold Bloods"},
    "Shun":            {"Desolate Plunderers", "Hook Raiders", "Tech Hunters"},
    "Border Zone":     {"United Cities", "Shek Kingdom", "Traders Guild",
                        "Tech Hunters", "Shinobi Thieves", "Nomads"},
    "The Hub":         {"Tech Hunters", "Shinobi Thieves", "Nomads"},
    # Isolated — only learn events if explicitly in factions_full
    "Cannibal Plains": set(),
    "Fog Islands":     set(),
    "The Gut":         set(),
    "Ashlands":        set(),
}

_KENSHI_REGION_NEIGHBORS = {
    "Holy Lands":      ["Great Desert", "Stenn Desert", "Border Zone"],
    "Great Desert":    ["Holy Lands", "South Wetlands", "Stenn Desert", "Border Zone", "The Hub"],
    "South Wetlands":  ["Great Desert", "The Swamp"],
    "Stenn Desert":    ["Great Desert", "Holy Lands", "Hook", "Black Desert", "Border Zone"],
    "Hook":            ["Stenn Desert", "Black Desert"],
    "Black Desert":    ["Stenn Desert", "Hook", "Iron Valleys", "Grey Desert"],
    "Iron Valleys":    ["Black Desert", "Border Zone"],
    "Grey Desert":     ["Black Desert", "Ashlands"],
    "The Swamp":       ["South Wetlands"],
    "Shun":            ["Stenn Desert", "Hook"],
    "Border Zone":     ["Holy Lands", "Great Desert", "Stenn Desert", "Iron Valleys", "The Hub"],
    "The Hub":         ["Great Desert", "Border Zone"],
    "Cannibal Plains": [], "Fog Islands": [], "The Gut": [], "Ashlands": ["Grey Desert"],
}

# These factions never receive vague awareness unless explicitly in factions_full
_ISOLATED_FACTIONS = frozenset({
    "Fogmen", "Cannibals", "Fishmen", "Skin Bandits",
    "Beak Things", "Spider Clan", "Reawakened", "Third Empire",
})

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_chronicle(cdir):
    """Load active events from campaign_chronicle.json. Returns [] on any error."""
    path = os.path.join(cdir, "campaign_chronicle.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"CHRONICLE: Failed to load chronicle: {e}")
        return []


def save_chronicle(cdir, events):
    """Persist events to campaign_chronicle.json. Archives overflow to chronicle_archive.json.
    Returns the (possibly trimmed) active list."""
    if len(events) > _CHRONICLE_MAX_ACTIVE:
        overflow = events[:len(events) - _CHRONICLE_MAX_ACTIVE]
        events = events[len(events) - _CHRONICLE_MAX_ACTIVE:]
        archive_path = os.path.join(cdir, "chronicle_archive.json")
        try:
            existing = []
            if os.path.exists(archive_path):
                with open(archive_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            existing.extend(overflow)
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            logging.info(f"CHRONICLE: Archived {len(overflow)} overflow events.")
        except Exception as e:
            logging.error(f"CHRONICLE: Failed to write archive: {e}")
    path = os.path.join(cdir, "campaign_chronicle.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"CHRONICLE: Failed to save chronicle: {e}")
    return events


def append_major_event(cdir, event_dict):
    """Load active chronicle, append event_dict, save. Returns updated active list."""
    events = load_chronicle(cdir)
    events.append(event_dict)
    return save_chronicle(cdir, events)

# ---------------------------------------------------------------------------
# Promotion: which live events deserve a place in history
# ---------------------------------------------------------------------------

# Kenshi за вечер выдаёт десятки ударов и нокаутов. Это тактика, а не история:
# город спустя неделю помнит, кто умер, кто сел и кому теперь принадлежат
# стены. Активных записей всего 15, поэтому пускать сюда бои нельзя — они
# вытеснят всё остальное за один налёт.
_MAJOR_EVENT_TYPES = frozenset({"death", "imprisonment", "city_transfer"})

# «Нет фракции» в роли прежнего владельца означает не захват, а первичную
# опись мира при загрузке сейва: таких событий приезжает больше двух сотен.
# "Безымянные"/"Nameless" сюда НЕ входит: это фракция отряда игрока по
# умолчанию и настоящая сущность базы, а не отсутствие фракции.
_EMPTY_FACTIONS = frozenset({
    "", "none", "nobody", "unknown", "n/a", "нет фракции", "неизвестно",
})

_PLAYER_SQUAD_MARK = "player's squad"

# Кто на кого напал: событие смерти виновника не содержит, а без виновника
# летопись не может сказать, что это сделал игрок.
_COMBAT_MEMORY = {}
_COMBAT_MEMORY_TTL = 180.0     # секунд между ударом и смертью, чтобы связать
_COMBAT_MEMORY_MAX = 400


def _clean(value) -> str:
    return str(value or "").strip()


def is_player_squad(faction) -> bool:
    """Отряд игрока помечается в событиях как "Player's Squad: <фракция>"."""
    return _clean(faction).lower().startswith(_PLAYER_SQUAD_MARK)


def plain_faction(faction) -> str:
    """"Player's Squad: Безымянные" -> "Безымянные"."""
    text = _clean(faction)
    if is_player_squad(text) and ":" in text:
        return text.split(":", 1)[1].strip()
    return text


def _is_empty_faction(faction) -> bool:
    return plain_faction(faction).lower() in _EMPTY_FACTIONS


def faction_key(name) -> str:
    """Ключ сравнения фракций: русское имя приводится к английскому, если можно."""
    text = plain_faction(name)
    if not text:
        return ""
    if _game_locale is not None:
        try:
            return _game_locale.to_english_key(text)
        except Exception:
            pass
    text = re.sub(r"[\s_-]+", " ", text.lower()).strip()
    return re.sub(r"^the\s+", "", text)


def _same_faction(a, b) -> bool:
    ka, kb = faction_key(a), faction_key(b)
    return bool(ka) and ka == kb


def note_combat(attacker, attacker_faction, victim):
    """Запомнить нападение, чтобы приписать смерти виновника."""
    victim_key = _clean(victim).lower()
    if not victim_key or not _clean(attacker):
        return
    if len(_COMBAT_MEMORY) > _COMBAT_MEMORY_MAX:
        _COMBAT_MEMORY.clear()
    _COMBAT_MEMORY[victim_key] = (_clean(attacker), _clean(attacker_faction), time.time())


def _recent_attacker(victim):
    entry = _COMBAT_MEMORY.get(_clean(victim).lower())
    if not entry:
        return None
    name, faction, when = entry
    if time.time() - when > _COMBAT_MEMORY_TTL:
        return None
    return name, faction


def _who(name, faction) -> str:
    name, faction = _clean(name), plain_faction(faction)
    if name and faction and not _is_empty_faction(faction):
        return f"{name} ({faction})"
    return name or faction or "кто-то"


def _town_from_message(message, fallback="") -> str:
    """"Town Squin changed ownership" -> "Squin"."""
    m = re.match(r"^Town\s+(.*?)\s+changed ownership\s*$", _clean(message), re.IGNORECASE)
    return m.group(1).strip() if m else _clean(fallback)


def _describe(etype, actor, actor_faction, target, target_faction, message, location):
    """Вернуть (summary, summary_vague, radius, факции_с_подробностями) или None."""
    where = _clean(location) or "пустошах"

    if etype == "death":
        killer = _recent_attacker(actor)
        factions = {plain_faction(actor_faction)}
        by_player = is_player_squad(actor_faction)
        if killer:
            killer_name, killer_faction = killer
            factions.add(plain_faction(killer_faction))
            by_player = by_player or is_player_squad(killer_faction)
            summary = (f"{_who(actor, actor_faction)} погиб в бою — "
                       f"убийца {_who(killer_name, killer_faction)}, место: {where}.")
        else:
            summary = f"{_who(actor, actor_faction)} погиб в {where}."
        vague = f"Поговаривают, в {where} кто-то расстался с жизнью."
        return summary, vague, ("regional" if by_player else "local"), factions

    if etype == "imprisonment":
        released = "releas" in _clean(message).lower()
        who = _who(actor, actor_faction)
        if released:
            summary = f"{who} вышел на свободу в {where}."
            vague = f"Поговаривают, в {where} кого-то выпустили из клетки."
        else:
            summary = f"{who} угодил за решётку в {where}."
            vague = f"Поговаривают, в {where} кого-то бросили в клетку."
        radius = "regional" if is_player_squad(actor_faction) else "local"
        return summary, vague, radius, {plain_faction(actor_faction)}

    if etype == "city_transfer":
        town = _town_from_message(message, location)
        old, new = plain_faction(actor_faction), plain_faction(target_faction)
        if not new or _is_empty_faction(new):
            return None
        summary = f"{town} перешёл под контроль: {new} отбили город у фракции {old}."
        vague = f"Говорят, {town} сменил хозяев."
        return summary, vague, "global", {old, new}

    return None


def consider_event(cdir, etype, actor, target, message,
                   actor_faction="", target_faction="",
                   location="", region="", day=None):
    """Записать событие в летопись, если оно того стоит.

    Возвращает добавленную запись или None. Ошибки не пробрасываются: летопись
    не должна ронять обработку игрового события.
    """
    try:
        etype = _clean(etype)
        if etype == "combat":
            note_combat(actor, actor_faction, target)
            return None
        if etype not in _MAJOR_EVENT_TYPES:
            return None
        # Смерть или арест без имени записывать не о чем.
        if etype in ("death", "imprisonment") and not _clean(actor):
            return None
        # Первичная опись мира при загрузке сейва, а не захват города.
        if etype == "city_transfer" and _is_empty_faction(actor_faction):
            return None

        described = _describe(etype, actor, actor_faction, target, target_faction,
                              message, location)
        if not described:
            return None
        summary, vague, radius, factions = described

        existing = load_chronicle(cdir)
        if any(_clean(e.get("summary")) == summary for e in existing):
            return None   # то же самое уже записано

        event = {
            "summary": summary,
            "summary_vague": vague,
            "factions_full": sorted(f for f in factions if f and not _is_empty_faction(f)),
            "radius": radius,
            "location": _clean(location),
            "location_region": _clean(region),
            "tags": [etype],
            "timestamp": time.time(),
        }
        if day is not None:
            try:
                event["day"] = int(day)
            except (TypeError, ValueError):
                pass

        existing.append(event)
        save_chronicle(cdir, existing)
        logging.info(f"CHRONICLE: {summary} (radius={radius}, "
                     f"factions={event['factions_full']})")
        return event
    except Exception as e:
        logging.warning(f"CHRONICLE: failed to record {etype}: {e}")
        return None


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def build_chronicle_block(npc_data, cdir, current_location=""):
    """Build the [CAMPAIGN CHRONICLE] block for this NPC.

    current_location — город, где идёт разговор. О случившемся здесь же
    говорят все местные, кем бы они ни были; без этого уровня убийство
    бандита знали бы только бандиты.
    Returns a formatted string, or '' if no relevant events exist.
    """
    npc_faction = _clean((npc_data or {}).get("Faction", ""))
    npc_key = faction_key(npc_faction)
    full_lines, vague_lines = [], []

    def _region_holds(region_name) -> bool:
        """Водится ли фракция NPC в этом регионе.

        Таблицы регионов записаны английскими именами, а игра присылает имена
        на языке игрока — поэтому сравниваем по нормализованному ключу.
        """
        for member in _KENSHI_REGION_FACTIONS.get(region_name, ()):  # noqa: SIM110
            if _same_faction(member, npc_faction):
                return True
        return False

    for event in load_chronicle(cdir):
        summary         = _clean(event.get("summary"))
        factions_full   = event.get("factions_full", [])
        radius          = event.get("radius", "local")
        location_region = event.get("location_region", "")
        location        = event.get("location") or "пустошах"
        day             = event.get("day", "")

        summary_vague = _clean(event.get("summary_vague")) or (
            f"Поговаривают, в {location} что-то стряслось"
            + (f" — и замешана {factions_full[0]}." if factions_full else ".")
        )
        day_prefix = f"День {day}: " if day else ""

        # Уровень А: названные фракции знают подробности
        if npc_key and any(_same_faction(f, npc_faction) for f in factions_full):
            full_lines.append(f"- {day_prefix}{summary}")
            continue

        # Отрезанные от мира не получают даже слухов
        if any(_same_faction(f, npc_faction) for f in _ISOLATED_FACTIONS):
            continue

        # Уровень Б: случилось прямо здесь — в городе об этом говорят.
        # Игра присылает регион равным названию города, поэтому радиусный
        # уровень ниже почти никогда не срабатывает, а этот — работает всегда.
        here = _clean(current_location)
        if here and _clean(location) and here.casefold() == _clean(location).casefold():
            vague_lines.append(f"- {day_prefix}{summary_vague}")
            continue

        # Уровень В: смутная осведомлённость по радиусу
        region_key = _clean(location_region)
        if _game_locale is not None and region_key:
            try:
                # "Пограничная зона" -> "Border Zone": таблицы ключуются так.
                region_key = _game_locale.to_english(region_key)
            except Exception:
                pass

        if radius == "global":
            vague_lines.append(f"- {day_prefix}{summary_vague}")
        elif radius == "regional":
            candidates = {region_key} | set(_KENSHI_REGION_NEIGHBORS.get(region_key, []))
            if any(_region_holds(r) for r in candidates):
                vague_lines.append(f"- {day_prefix}{summary_vague}")
        elif radius == "local":
            if _region_holds(region_key):
                vague_lines.append(f"- {day_prefix}{summary_vague}")

    if not full_lines and not vague_lines:
        return ""
    parts = ["[ЛЕТОПИСЬ КАМПАНИИ]"]
    if full_lines:
        parts += ["События, которым ты был свидетелем или о которых знаешь наверняка:"] + full_lines
    if vague_lines:
        parts += ["Слухи, донёсшиеся издалека:"] + vague_lines
    return "\n".join(parts)
