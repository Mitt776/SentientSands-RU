# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak retriever.

Layer 0  → direct keyword / name matches against the index.
Layer N+ → open each matched entity, scan field values, try to match
           any value against the name index, enqueue new matches.

No recognized_fields whitelist. Every field value is a valid expansion
candidate EXCEPT:
  • fields in non_expand_fields config (structural: id, weight, etc.)
  • fields identified as text fields by _is_text_field() convention
    (text_ prefix or _desc/_note/_summary etc. suffix)

This prevents prose from accidentally triggering expansion
(e.g. text_personality = "devout follower of Okran" won't expand to Okran)
while keeping the system fully open for world reference fields.

Stop conditions: max_layers, max_files, timeout_ms.
An entity is never returned twice per retrieval cycle.
"""

import re
import time
from collections import defaultdict
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .indexer import (Indexer, Entity, AccessRule, _norm, _tokenize,
                      _is_text_field, stem_key)

_STOP = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "shall", "should", "may", "might", "can", "could",
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
    "that", "this", "these", "those",
    "and", "or", "but", "if", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "into", "about",
    "think", "say", "said", "hear", "heard", "know", "want",
    "ever", "never", "also", "just", "where", "what", "who",
    "how", "when", "why", "not", "no", "yes", "all", "any",
    "some", "there", "then", "than", "so", "as", "up", "out",
    "been", "each", "very",
})

# Русские служебные слова. Без них «Расскажи», «что» и даже одинокая «А»
# занимали места среди трёх ключей, которые достаются поиску по лору.
# Сюда же глаголы обращения: разговор с NPC почти всегда начинается с них,
# а имени в них нет.
_STOP_RU = frozenset({
    # местоимения
    "я", "ты", "он", "она", "оно", "мы", "вы", "они", "меня", "тебя", "его",
    "её", "ее", "нас", "вас", "их", "мне", "тебе", "ему", "ей", "нам", "вам",
    "им", "мной", "тобой", "себя", "себе", "сам", "сама", "сами",
    # притяжательные и указательные
    "мой", "моя", "моё", "мое", "мои", "твой", "твоя", "твоё", "твое", "твои",
    "наш", "наша", "наши", "ваш", "ваша", "ваши", "свой", "своя", "свои",
    "этот", "эта", "это", "эти", "тот", "та", "то", "те", "такой", "такая",
    "такое", "такие",
    # вопросы
    "что", "кто", "где", "куда", "откуда", "когда", "как", "почему", "зачем",
    "какой", "какая", "какое", "какие", "сколько", "чей", "чего", "чем",
    "кого", "кому", "чём", "чем-то", "что-то", "что-нибудь", "кто-нибудь",
    # союзы, частицы, предлоги
    "и", "а", "но", "или", "либо", "да", "нет", "не", "ни", "же", "ли", "бы",
    "ведь", "вот", "уж", "разве", "неужели", "если", "чтобы", "хотя", "тоже",
    "также", "ещё", "еще", "уже", "только", "просто", "очень", "совсем",
    "в", "во", "на", "за", "из", "от", "до", "по", "под", "над", "при",
    "про", "для", "без", "к", "ко", "с", "со", "у", "о", "об", "обо",
    "через", "между", "около", "перед", "после",
    # наречия места и времени
    "там", "тут", "здесь", "сюда", "туда", "сейчас", "потом", "теперь",
    "всегда", "никогда", "нынче",
    # обращение к собеседнику: с них начинается почти каждая реплика
    "расскажи", "скажи", "говори", "говорит", "говорят", "знаешь", "знаю",
    "знает", "знаете", "слышал", "слышала", "слышали", "слышишь", "думаешь",
    "думаю", "помнишь", "помню", "подскажи", "ответь", "спроси", "объясни",
    "привет", "здравствуй", "здорово", "слушай", "слышь", "давай", "ладно",
    "пожалуйста", "спасибо",
    # прочее частое
    "всё", "все", "весь", "вся", "ничего", "что-нибудь", "кое-что",
    "стоит", "надо", "нужно", "можно", "хочу", "хочешь",
})

_STOP = _STOP | _STOP_RU


class Retriever:
    def __init__(self, indexer: Indexer, config: dict):
        self.indexer      = indexer
        self.config       = config
        self._non_expand: FrozenSet[str] = config.get("non_expand_fields", frozenset())

    def retrieve(
        self,
        keywords:      List[str],
        priority_uids: Optional[List[str]] = None,
        knower_context: Optional[Dict[str, Any]] = None,
        disable_knowledge_rules: bool = False,
        exempt_uids: Optional[List[str]] = None,
    ):
        """
        Return (entities, meta) where meta is a dict with timing and config info.

        meta keys: elapsed_ms, timeout_hit, entities_found, config_snapshot
        priority_uids: always included and seeded for expansion first.
        (e.g. target NPC — ensure it's in results even if not keyword-matched)
        """
        _t_start = time.monotonic()
        _timeout_hit = False
        max_kw     = self.config.get("max_keywords",           5)
        max_layers = self.config.get("max_layers",             3)
        max_mpl    = self.config.get("max_matches_per_layer",  5)
        max_files  = self.config.get("max_files",             10)
        timeout_ms = self.config.get("timeout_ms",          1500)

        deadline                        = time.monotonic() + timeout_ms / 1000.0
        exempt_set: Set[str]            = set(exempt_uids or [])
        best_scores: Dict[str, float]   = {}
        queue:  List[Tuple[str, int, float]] = []
        expanded: Set[str]              = set()
        priority_added: Set[str]        = set()
        compiled_knower                 = _compile_knower_context(knower_context or {})

        def _non_priority_count() -> int:
            return max(0, len(best_scores) - len(priority_added))

        def _can_access(uid: str) -> bool:
            if disable_knowledge_rules or uid in exempt_set:
                return True
            entity = self.indexer.get(uid)
            return _entity_access_allowed(entity, compiled_knower)

        def _add(uid: str, score: float, depth: int, chain_weight: float = 1.0):
            if not _can_access(uid):
                return False
            previous = best_scores.get(uid)
            if previous is None:
                best_scores[uid] = score
                queue.append((uid, depth, chain_weight))
                return True
            if score > previous:
                best_scores[uid] = score
            return False

        # ── Priority entities (always included, highest score) ────────────
        n_priority = 0
        for uid in (priority_uids or []):
            ent = self.indexer.get(uid)
            if ent:
                if _add(uid, ent.weight + 1000, 0, chain_weight=1.0):
                    n_priority += 1
                    priority_added.add(uid)

        # ── Layer 0: direct keyword / name matches ────────────────────────
        for kw in keywords[:max_kw]:
            if time.monotonic() > deadline:
                _timeout_hit = True
                break
            norm = _norm(kw)
            for uid in (self.indexer.find_by_name(kw)
                        or self.indexer.find_by_stem(kw)):
                ent = self.indexer.get(uid)
                _add(uid, (ent.weight if ent else 5) + 50, 0, chain_weight=1.0)
                if _non_priority_count() >= max_files:
                    break
            if _non_priority_count() >= max_files:
                break
            for uid in self.indexer.find_by_keyword(norm):
                ent = self.indexer.get(uid)
                _add(uid, ent.weight if ent else 5, 0, chain_weight=1.0)
                if _non_priority_count() >= max_files:
                    break
            if _non_priority_count() >= max_files:
                break

        # ── Layer expansion ───────────────────────────────────────────────
        qi = 0
        while qi < len(queue) and _non_priority_count() < max_files:
            if time.monotonic() > deadline:
                _timeout_hit = True
                break
            uid, depth, chain_weight = queue[qi]
            qi += 1

            if depth >= max_layers:
                continue

            entity = self.indexer.get(uid)
            if not entity or uid in expanded:
                continue
            expanded.add(uid)

            # Preferred future expansion path:
            # define_children.txt is indexed as entity.child_links. The new
            # token_resolver.py uses ONLY this path for child entity expansion.
            if entity.child_links:
                matches_this = 0
                for child in entity.child_links:
                    if time.monotonic() > deadline:
                        _timeout_hit = True
                        break
                    if matches_this >= max_mpl or _non_priority_count() >= max_files:
                        break
                    cent = self.indexer.get(child.target_uid)
                    child_chain = chain_weight * child.weight
                    score = (cent.weight if cent else 5) + (child_chain * 40.0)
                    _add(child.target_uid, score, depth + 1, chain_weight=child_chain)
                    matches_this += 1
                continue

            # LEGACY FIELD-BASED EXPANSION.
            # This scans arbitrary structured fields and tries to resolve their values
            # as more entities. It is retained temporarily for backwards compatibility,
            # but the token-driven prompt system must not call this behavior. Future
            # retrieval should expand only through define_children.txt / child_links.
            matches_this = 0
            for fkey, fval in entity.fields.items():
                if time.monotonic() > deadline:
                    _timeout_hit = True
                    break
                if matches_this >= max_mpl:
                    break

                # Skip structural fields from config
                if fkey.lower() in self._non_expand:
                    continue

                # Skip text/prose fields — they contain human language,
                # not world references, and would generate noisy expansions.
                if _is_text_field(fkey):
                    continue

                # Values may be comma-separated and may also use spaced "&"
                # as a human-friendly separator. We only split on spaced "&"
                # so literal names like "R&D_Corp" are preserved.
                for raw_ref in re.split(r",|\s+&\s+", fval):
                    ref = raw_ref.strip()
                    if not ref or len(ref) < 2:
                        continue
                    for cuid in self.indexer.find_by_name(ref):
                        cent  = self.indexer.get(cuid)
                        score = (cent.weight if cent else 5) * (0.75 ** depth)
                        _add(cuid, score, depth + 1, chain_weight=chain_weight)
                        matches_this += 1
                        if _non_priority_count() >= max_files:
                            break
                    if _non_priority_count() >= max_files:
                        break

        scored = sorted(
            best_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        results = [ent for uid, _ in scored if (ent := self.indexer.get(uid))]
        _elapsed = round((time.monotonic() - _t_start) * 1000, 2)
        meta = {
            "elapsed_ms":    _elapsed,
            "timeout_hit":   _timeout_hit,
            "entities_found": len(results),
            "config_snapshot": {
                "max_keywords":          max_kw,
                "max_layers":            max_layers,
                "max_matches_per_layer": max_mpl,
                "max_files":             max_files,
                "timeout_ms":            timeout_ms,
            },
        }
        return results, meta


# ─── KEYWORD EXTRACTION ──────────────────────────────────────────────────────

def _canonical_name(indexer: Optional[Indexer], phrase: str) -> str:
    """Имя сущности по фразе игрока — точно, иначе с учётом падежа.

    Возвращается имя, как оно записано в базе: дальше по цепочке работает
    обычный точный поиск, и падежи больше нигде знать не нужно.
    """
    if not indexer or not phrase:
        return ""
    if indexer.find_by_name(phrase):
        return phrase
    uids = indexer.find_by_stem(phrase)
    if not uids:
        return ""
    entity = indexer.get(uids[0])
    return entity.name if entity else ""


def extract_keywords(
    text:         str,
    indexer:      Optional[Indexer],
    max_keywords: int = 5,
) -> List[str]:
    """
    Extract meaningful keywords from player input.

    Priority:
      1. 3-grams / 2-grams matched against indexed entity names
         (catches "Blister Hill", "Holy Nation", "Emperor Tengu")
      2. Capitalised tokens not in stop list
      3. All non-stop tokens
    """
    words = [
        w.strip(".,;:!?'\"()[]{}") for w in text.split()
        if w.strip(".,;:!?'\"()[]{}")
    ]

    found:     List[str] = []
    found_set: Set[str]  = set()

    def _push(w: str) -> bool:
        k = _norm(w)
        if k and k not in found_set:
            found_set.add(k)
            found.append(w)
            return len(found) >= max_keywords
        return False

    # Слова, ушедшие в найденное имя. Без этого «Святой Нации» находилось
    # целиком, а два оставшихся места забирали его же половинки, которые сами
    # по себе не находят ничего.
    consumed: Set[int] = set()

    if indexer:
        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                if any(j in consumed for j in range(i, i + n)):
                    continue
                phrase = " ".join(words[i : i + n])
                if n == 1 and phrase.lower() in _STOP:
                    continue
                canonical = _canonical_name(indexer, phrase)
                if not canonical:
                    continue
                consumed.update(range(i, i + n))
                if _push(canonical):
                    return found

    for i, w in enumerate(words):
        if i in consumed:
            continue
        clean = w.strip(".,;:!?'\"")
        if clean and clean[0].isupper() and clean.lower() not in _STOP:
            if _push(clean):
                return found

    for i, w in enumerate(words):
        if i in consumed:
            continue
        clean = w.strip(".,;:!?'\"").lower()
        if len(clean) > 2 and clean not in _STOP:
            if _push(clean):
                return found

    return found


def _entity_access_allowed(entity: Optional[Entity], compiled_knower: Dict[str, Set[str]]) -> bool:
    if not entity or entity.bypass_filter:
        return True
    if not entity.access_rule_groups:
        return True
    for rule_group in entity.access_rule_groups:
        if not any(_rule_matches(rule, compiled_knower) for rule in rule_group):
            return False
    return True


def _rule_matches(rule: AccessRule, compiled_knower: Dict[str, Set[str]]) -> bool:
    for condition in rule.conditions:
        actual = compiled_knower.get(condition.key)
        if not actual:
            return False
        if actual.isdisjoint(condition.values):
            return False
    return True


def _compile_knower_context(context: Dict[str, Any]) -> Dict[str, Set[str]]:
    compiled: Dict[str, Set[str]] = defaultdict(set)

    def _add_value(key: str, value: Any):
        key_norm = _norm(key)
        if not key_norm or value is None:
            return
        if isinstance(value, dict):
            for subkey, subvalue in value.items():
                _add_value(subkey, subvalue)
                _add_value(f"{key}_{subkey}", subvalue)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add_value(key, item)
            return

        text = str(value).strip()
        if not text:
            return
        for raw_part in re.split(r"[|,\n]+", text):
            normalized = _norm(raw_part)
            if normalized:
                compiled[key_norm].add(normalized)

    for key, value in (context or {}).items():
        _add_value(key, value)

    _alias_context_key(compiled, "town_name", "city", "town", "location")
    _alias_context_key(compiled, "home_region", "region")
    _alias_context_key(compiled, "location_name", "location", "city")
    _alias_context_key(compiled, "environment_town_name", "city", "town", "location")
    _alias_context_key(compiled, "environment_region", "region")
    return dict(compiled)


def _alias_context_key(compiled: Dict[str, Set[str]], source_key: str, *aliases: str):
    source = compiled.get(_norm(source_key))
    if not source:
        return
    for alias in aliases:
        compiled.setdefault(_norm(alias), set()).update(source)
