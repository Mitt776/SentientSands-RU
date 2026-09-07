# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak indexer.

TEXT FIELD CONVENTION:
  Fields whose name starts with "$" contain free-form prose and are
  NEVER used for layer expansion (they might reference world names accidentally).
  Examples: $personality, $backstory, $bio, $speech_quirks
  By default they are NOT keyword-indexed (to prevent noisy matches from prose).
  This can be toggled with config: index_text_fields = true
  The $ prefix is stripped when rendering to the prompt so the LLM sees
  clean field names like "personality: Disciplined, severe..."

MATCHING RULE OF THUMB:
  Non-$ field values are matchable (player-editable world keys).
  $ fields are prompt-facing prose by default (not keyword-matched).

ID MODEL (SentientSands-aware):
  entity.txt body supports two named ID fields:
    persistent_id  — stable across saves (Kenshi 5-part UUID)
    runtime_id     — session-scoped handle, changes between game loads
  Both are indexed separately. The header "Id" = most stable ID available.

INDEX vs PROMPT-TIME READS (reindex policy):
  entity.txt, who_knows_me.txt, and define_children.txt changes require a reindex.
  stats.txt, dialogue.txt, price.txt etc. are read fresh at prompt-time.
  Use add_or_update_entity(entity_dir) for incremental updates.
"""

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .normalizer import parse_entity_file

_CONTROL_MARKER_RE = re.compile(r"(>>|<<|\(\(|\)\)|@)")
_RULE_HEADER_RE = re.compile(r"^\[\s*RULE\s*\]$", re.IGNORECASE)
_CHILD_LINK_RE = re.compile(r"^W\s*=\s*([0-9]*\.?[0-9]+)\s+(.+?)\s*$", re.IGNORECASE)
_COMMENT_PREFIXES = ("#", ";", "//")

log = logging.getLogger("kayak.indexer")

# ─── FIELD CLASSIFICATION ────────────────────────────────────────────────────

def _is_text_field(key: str) -> bool:
    """
    True if this field contains prose — never used for layer expansion.
    Keyword indexing is controlled separately via config (index_text_fields).

    Convention: prefix the field name with $ in entity.txt.
    Examples:  $personality, $backstory, $bio, $speech_quirks

    The $ is stripped when the field is rendered into the prompt,
    so the LLM sees clean field names.
    """
    return key.startswith("$")


# ─── ENTITY MODEL ────────────────────────────────────────────────────────────

@dataclass
class Entity:
    uid:           str
    category:      str
    name:          str
    entity_id:     str              # most stable ID (from header or promoted from body)
    persistent_id: str              # Kenshi stable 5-part UUID
    runtime_id:    str              # session-scoped handle
    fields:        Dict[str, str]
    free_text:     str
    path:          str
    weight:        float = 5.0
    bypass_filter: bool = False
    mtime:         float = 0.0      # entity.txt last-modified timestamp (epoch)
    access_rule_groups: Tuple[Tuple["AccessRule", ...], ...] = field(default_factory=tuple)
    child_links:        Tuple["ChildLink", ...] = field(default_factory=tuple)
    raw_child_refs:     Tuple[Tuple[float, str], ...] = field(default_factory=tuple)

    @property
    def display_name(self) -> str:
        return self.fields.get("display_name") or self.name.replace("_", " ")

    @property
    def best_id(self) -> str:
        return self.persistent_id or self.entity_id or self.runtime_id


@dataclass(frozen=True)
class AccessCondition:
    key: str
    values: Tuple[str, ...]


@dataclass(frozen=True)
class AccessRule:
    conditions: Tuple[AccessCondition, ...]


@dataclass(frozen=True)
class ChildLink:
    target_uid: str
    target_name: str
    weight: float


# ─── INDEXER ─────────────────────────────────────────────────────────────────

class Indexer:
    def __init__(self, categories_path: str, config: dict):
        self.categories_path      = categories_path
        self.config               = config
        self.entities:            Dict[str, Entity]    = {}
        self.name_index:          Dict[str, List[str]] = defaultdict(list)
        # основа имени → нормализованные имена, которые её дали
        self._stem_to_names:      Dict[str, Set[str]]  = defaultdict(set)
        self.keyword_index:       Dict[str, Set[str]]  = defaultdict(set)
        self.id_index:            Dict[str, str]       = {}
        self.persistent_id_index: Dict[str, str]       = {}
        self.runtime_id_index:    Dict[str, str]       = {}
        self._path_index:         Dict[str, str]       = {}  # entity_dir → uid
        self._access_file_cache:  Dict[str, Tuple[AccessRule, ...]] = {}
        self._child_file_cache:   Dict[str, Tuple[Tuple[float, str], ...]] = {}
        self._ready = False

    def build(self) -> "Indexer":
        """Full (re)index from categories_path."""
        self.entities.clear()
        self.name_index.clear()
        self._stem_to_names.clear()
        self.keyword_index.clear()
        self.id_index.clear()
        self.persistent_id_index.clear()
        self.runtime_id_index.clear()
        self._path_index.clear()
        self._access_file_cache.clear()
        self._child_file_cache.clear()
        self._ready = False
        if os.path.isdir(self.categories_path):
            self._walk(self.categories_path, parent_category=None)
        self._finalize_metadata()
        self._ready = True
        return self

    def add_or_update_entity(self, entity_dir: str) -> Optional[str]:
        """
        Incrementally index or re-index a single entity folder.
        Call after writing entity.txt — NOT needed for stats/dialogue changes.
        Returns uid or None if no entity.txt found.
        """
        if not os.path.isfile(os.path.join(entity_dir, "entity.txt")):
            return None
        if entity_dir in self._path_index:
            self._remove_entity(self._path_index[entity_dir])
        category = os.path.basename(os.path.dirname(entity_dir))
        name     = os.path.basename(entity_dir)
        uid = self._index_entity(entity_dir, category, name)
        self._finalize_metadata()
        return uid

    def find_by_name(self, name: str) -> List[str]:
        seen: Set[str] = set()
        result = []
        for uid in self.name_index.get(_norm(name), []):
            if uid not in seen:
                seen.add(uid)
                result.append(uid)
        return result

    def find_by_stem(self, name: str) -> List[str]:
        """Поиск имени в косвенном падеже: «Чёрную Ссадину» → Black_Scratch.

        Отказывается отвечать, когда основа ведёт к разным именам: подставить
        наугад значит выдать NPC чужой лор. Мёртвые записи отсеиваются по
        живому name_index, поэтому отдельной чистки при удалении не нужно.
        """
        stem = stem_key(name)
        if not stem:
            return []
        names = [n for n in self._stem_to_names.get(stem, ()) if self.name_index.get(n)]
        if len(names) != 1:
            return []
        return list(self.name_index.get(names[0], []))

    def find_by_keyword(self, token: str) -> Set[str]:
        return self.keyword_index.get(_norm(token), set())

    def find_by_id(self, entity_id: str) -> Optional[str]:
        """Resolve any known ID to a uid. Prefers persistent > runtime > generic."""
        return (
            self.persistent_id_index.get(entity_id)
            or self.runtime_id_index.get(entity_id)
            or self.id_index.get(entity_id)
        )

    def find_by_persistent_id(self, pid: str) -> Optional[str]:
        return self.persistent_id_index.get(pid)

    def find_by_runtime_id(self, rid: str) -> Optional[str]:
        return self.runtime_id_index.get(rid)

    def get(self, uid: str) -> Optional[Entity]:
        return self.entities.get(uid)

    def stats(self) -> dict:
        return {
            "entities":       len(self.entities),
            "name_entries":   len(self.name_index),
            "keyword_tokens": len(self.keyword_index),
        }

    def _walk(self, current_dir: str, parent_category: Optional[str]):
        try:
            entries = sorted(os.listdir(current_dir))
        except PermissionError:
            return
        if "entity.txt" in entries:
            cat  = parent_category or os.path.basename(os.path.dirname(current_dir))
            name = os.path.basename(current_dir)
            self._index_entity(current_dir, cat, name)
            return
        for entry in entries:
            if entry.startswith("IGN_"):
                continue
            full = os.path.join(current_dir, entry)
            if os.path.isdir(full):
                new_cat = parent_category if parent_category is not None else entry
                self._walk(full, new_cat)

    def _index_entity(self, entity_dir: str, category: str, folder_name: str) -> str:
        header, fields, free_text = parse_entity_file(
            os.path.join(entity_dir, "entity.txt")
        )

        resolved_category = _strip_bypass_prefix(header.get("Category") or category)
        resolved_name     = _strip_bypass_prefix(header.get("Name") or folder_name)
        entity_id         = header.get("Id")       or ""
        bypass_filter     = _path_has_bypass_prefix(entity_dir, self.categories_path)

        # Pull identity fields out of body (they don't belong in expansion)
        persistent_id = fields.pop("persistent_id", "") or fields.pop("persistentid", "")
        runtime_id    = fields.pop("runtime_id",    "") or fields.pop("runtimeid",    "")

        if not entity_id:
            entity_id = persistent_id or runtime_id

        uid = self._unique_uid(f"{resolved_category}/{resolved_name}", entity_dir)

        try:
            weight = float(fields.get("weight", "5"))
        except ValueError:
            weight = 5.0

        # Read entity.txt modification time for sorting support
        entity_file = os.path.join(entity_dir, "entity.txt")
        try:
            mtime = os.path.getmtime(entity_file)
        except OSError:
            mtime = 0.0

        entity = Entity(
            uid           = uid,
            category      = resolved_category,
            name          = resolved_name,
            entity_id     = entity_id,
            persistent_id = persistent_id,
            runtime_id    = runtime_id,
            fields        = fields,
            free_text     = free_text,
            path          = entity_dir,
            weight        = weight,
            bypass_filter = bypass_filter,
            mtime         = mtime,
            access_rule_groups = self._compile_access_rule_groups(entity_dir),
            raw_child_refs     = self._parse_child_refs(entity_dir),
        )

        self.entities[uid]           = entity
        self._path_index[entity_dir] = uid

        # Name index
        for n in self._all_names(entity):
            key = _norm(n)
            self.name_index[key].append(uid)
            stem = stem_key(n)
            if stem:
                self._stem_to_names[stem].add(key)

        # ID indexes
        for id_val, idx in (
            (entity_id,     self.id_index),
            (persistent_id, self.persistent_id_index),
            (runtime_id,    self.runtime_id_index),
        ):
            if id_val:
                idx[id_val] = uid
        if persistent_id:
            self.id_index[persistent_id] = uid
        if runtime_id:
            self.id_index[runtime_id] = uid

        # Keyword index policy:
        # - Always index resolved_name
        # - Index non-text fields
        # - Optionally index $text fields and free_text via config
        include_text_fields = bool(self.config.get("index_text_fields", False))
        include_free_text = bool(self.config.get("index_free_text", False))

        corpus_parts = [resolved_name]
        for fkey, fval in fields.items():
            if _is_text_field(fkey) and not include_text_fields:
                continue
            corpus_parts.append(fval)
        if include_free_text and free_text:
            corpus_parts.append(free_text)

        corpus = " ".join(corpus_parts)
        for token in _tokenize(corpus):
            self.keyword_index[token].add(uid)

        return uid

    def _finalize_metadata(self):
        for entity in self.entities.values():
            resolved_links: List[ChildLink] = []
            for weight, target_name in entity.raw_child_refs:
                candidates = self.find_by_name(target_name)
                if not candidates:
                    log.warning(
                        "define_children unresolved for %s: %s",
                        entity.uid,
                        target_name,
                    )
                    continue
                target_uid = candidates[0]
                target_entity = self.get(target_uid)
                if not target_entity:
                    continue
                resolved_links.append(
                    ChildLink(
                        target_uid=target_uid,
                        target_name=target_entity.name,
                        weight=weight,
                    )
                )
            resolved_links.sort(key=lambda item: item.weight, reverse=True)
            entity.child_links = tuple(resolved_links)

    def _remove_entity(self, uid: str):
        entity = self.entities.pop(uid, None)
        if not entity:
            return
        self._path_index.pop(entity.path, None)
        for key, uids in list(self.name_index.items()):
            cleaned = [u for u in uids if u != uid]
            if cleaned:
                self.name_index[key] = cleaned
            else:
                del self.name_index[key]
        for key, uid_set in list(self.keyword_index.items()):
            uid_set.discard(uid)
            if not uid_set:
                del self.keyword_index[key]
        for index in (self.id_index, self.persistent_id_index, self.runtime_id_index):
            for k, v in list(index.items()):
                if v == uid:
                    del index[k]

    def _unique_uid(self, base: str, entity_dir: str) -> str:
        if base not in self.entities:
            return base
        folder    = os.path.basename(entity_dir)
        candidate = f"{base}__{folder}"
        counter   = 2
        while candidate in self.entities:
            candidate = f"{base}__{folder}_{counter}"
            counter  += 1
        return candidate

    def _all_names(self, entity: Entity) -> List[str]:
        names = [entity.name]
        dn = entity.fields.get("display_name", "")
        if dn and dn != entity.name:
            names.append(dn)
        for alias in re.split(r"[|,]", entity.fields.get("aliases", "")):
            a = alias.strip()
            if a:
                names.append(a)
        return names

    def _compile_access_rule_groups(self, entity_dir: str) -> Tuple[Tuple[AccessRule, ...], ...]:
        groups: List[Tuple[AccessRule, ...]] = []
        rel_path = os.path.relpath(entity_dir, self.categories_path)
        current = self.categories_path
        if os.path.normcase(rel_path) in (".", ""):
            path_chain = [current]
        else:
            path_chain = [current]
            for part in rel_path.split(os.sep):
                current = os.path.join(current, part)
                path_chain.append(current)

        for directory in path_chain:
            rules = self._parse_access_rules_file(directory)
            if rules:
                groups.append(rules)

        return tuple(groups)

    def _parse_access_rules_file(self, directory: str) -> Tuple[AccessRule, ...]:
        path = os.path.join(directory, "who_knows_me.txt")
        cached = self._access_file_cache.get(path)
        if cached is not None:
            return cached
        if not os.path.isfile(path):
            self._access_file_cache[path] = tuple()
            return tuple()

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = [line.rstrip("\n") for line in handle]
        except OSError as exc:
            log.warning("Failed to read who_knows_me.txt at %s: %s", path, exc)
            self._access_file_cache[path] = tuple()
            return tuple()

        rules: List[AccessRule] = []
        current_conditions: List[AccessCondition] = []
        saw_content = False

        def _flush_rule():
            nonlocal current_conditions
            if current_conditions:
                rules.append(AccessRule(conditions=tuple(current_conditions)))
                current_conditions = []

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(_COMMENT_PREFIXES):
                continue
            if _RULE_HEADER_RE.match(stripped):
                saw_content = True
                _flush_rule()
                continue
            if "=" not in stripped:
                saw_content = True
                log.warning("Ignoring malformed who_knows_me line in %s: %s", path, stripped)
                continue

            key, _, value = stripped.partition("=")
            key_norm = _norm_rule_key(key)
            values = tuple(
                normalized
                for normalized in (_norm_rule_value(part) for part in value.split("|"))
                if normalized
            )
            saw_content = True
            if not key_norm or not values:
                log.warning("Ignoring empty who_knows_me rule in %s: %s", path, stripped)
                continue
            current_conditions.append(AccessCondition(key=key_norm, values=values))

        _flush_rule()

        if not rules:
            if saw_content:
                log.warning(
                    "No valid who_knows_me rules compiled for %s; treating as unrestricted.",
                    path,
                )
            self._access_file_cache[path] = tuple()
            return tuple()

        compiled = tuple(rules)
        self._access_file_cache[path] = compiled
        return compiled

    def _parse_child_refs(self, entity_dir: str) -> Tuple[Tuple[float, str], ...]:
        path = os.path.join(entity_dir, "define_children.txt")
        cached = self._child_file_cache.get(path)
        if cached is not None:
            return cached
        if not os.path.isfile(path):
            self._child_file_cache[path] = tuple()
            return tuple()

        refs: List[Tuple[float, str]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = [line.rstrip("\n") for line in handle]
        except OSError as exc:
            log.warning("Failed to read define_children.txt at %s: %s", path, exc)
            self._child_file_cache[path] = tuple()
            return tuple()

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(_COMMENT_PREFIXES):
                continue
            match = _CHILD_LINK_RE.match(stripped)
            if not match:
                log.warning("Ignoring malformed define_children line in %s: %s", path, stripped)
                continue
            try:
                weight = float(match.group(1))
            except ValueError:
                log.warning("Ignoring malformed define_children weight in %s: %s", path, stripped)
                continue
            if weight < 0.0 or weight > 1.0:
                clamped = max(0.0, min(1.0, weight))
                log.warning(
                    "Clamping define_children weight for %s from %.3f to %.3f",
                    path,
                    weight,
                    clamped,
                )
                weight = clamped
            target_name = match.group(2).strip()
            if not target_name:
                continue
            refs.append((weight, target_name))

        compiled = tuple(refs)
        self._child_file_cache[path] = compiled
        return compiled


# ─── SHARED UTILITIES ────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """
    Normalizes a name or keyword for indexing/lookup.
    Matches the aggressive cleaning in SentientSandsBridge._to_folder_name
    to ensure consistent identity resolution across the whole pipeline.
    """
    if not text:
        return ""
    # 1. Aggressive cleaning (match bridge's re.sub(r"[^\w\-]", "_", ...))
    clean = re.sub(r"[^\w\-]", "_", text.strip().lower())
    # 2. Collapse double underscores and strip ends (pragmatic normalization)
    return re.sub(r"_+", "_", clean).strip("_")


_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

# Окончания русских существительных и прилагательных. Отсекаем только их:
# «Чёрную Ссадину» и «Чёрная Ссадина» должны сойтись на «черн_ссадин».
# Полноценной морфологии тут нет и не нужно — задача узкая: узнать имя
# собственное в косвенном падеже.
_RU_ENDINGS = tuple(sorted({
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ах", "ях", "ов", "ев", "ам", "ям", "ая", "яя", "ое", "ее",
    "ый", "ий", "ой", "ым", "им", "ом", "ем", "ую", "юю", "ые", "ие", "ей",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
}, key=len, reverse=True))

# Короткое слово без окончания уже не слово: «Улей» -> «Ул» слилось бы с чем
# угодно. Три буквы — предел, ниже которого основу не режем.
_MIN_STEM = 3


def _stem_ru(word: str) -> str:
    """Слово без падежного окончания. Английское возвращается нетронутым."""
    if not _CYRILLIC_RE.search(word):
        return word
    for ending in _RU_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= _MIN_STEM:
            return word[: -len(ending)]
    return word


def stem_key(text: str) -> str:
    """Ключ поиска, нечувствительный к падежу и к букве «ё»."""
    parts = [p for p in _norm(text).replace("\u0451", "\u0435").split("_") if p]
    if not parts:
        return ""
    return "_".join(_stem_ru(p) for p in parts)


def _strip_bypass_prefix(text: str) -> str:
    """
    DEBUG MODE: b_ bypass workaround disabled.
    Keep names untouched while debugging so folder naming behavior is explicit.
    """
    return str(text or "").strip()


def _path_has_bypass_prefix(entity_dir: str, categories_root: str) -> bool:
    """
    DEBUG MODE: b_ bypass workaround disabled.
    Always return False so no entity bypasses knowledge filtering by folder prefix.
    """
    return False


def _tokenize(text: str) -> List[str]:
    # Strip lightweight Kayak control markers so they don't become noisy tokens.
    text = _CONTROL_MARKER_RE.sub(" ", text)
    text = text.replace("&", " ").replace("=", " ")
    return [
        t for t in
        (
            w.strip().lower().strip(".'\"")
            for w in re.split(r"[\s,;:!?()\[\]{}/\\<>|]+", text)
        )
        if len(t) > 1
    ]


def _norm_rule_key(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w\-]", "_", str(text or "").strip().lower())).strip("_")


def _norm_rule_value(text: str) -> str:
    return _norm(text)
