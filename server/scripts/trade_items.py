"""
trade_items.py — alias-only item name normalization for the action parser.
"""
import json
import logging
import re

_ALIASES: dict = {}
_ARTICLES = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def load_item_aliases(path: str) -> None:
    global _ALIASES
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _ALIASES = {k.strip().lower(): v for k, v in raw.items() if not k.startswith("_")}
        logging.debug(f"Loaded {len(_ALIASES)} item aliases from {path}")
    except Exception as e:
        logging.warning(f"Could not load item_aliases.json ({e}) — item normalization disabled")


def _clean(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(".,;:!?\'\"()[]{}")
    s = _ARTICLES.sub("", s)
    return s


def normalize_trade_item_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return raw

    key = _clean(raw)
    canonical = _ALIASES.get(key)
    if canonical and canonical != raw:
        logging.info(f"TRADE: normalized item alias '{raw}' -> '{canonical}'")
        return canonical
    return raw
