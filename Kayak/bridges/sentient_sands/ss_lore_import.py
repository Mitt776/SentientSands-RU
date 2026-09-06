# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
SentientSands World_lore.json → Kayak entity importer.

Converts the tag-based lore system into Kayak's layered retrieval system.
Instead of matching tags and injecting fixed lore chunks, Kayak will
naturally retrieve relevant lore through its expansion engine.

Example: player mentions "Holy Nation" →
  Layer 0: finds lore_faction/lore_holy_nation_overview entity
  Layer 1: that entity has faction = Holy_Nation, leader = Holy_Phoenix →
           expands to find base_npcs/Holy_Phoenix and lore_theology/okran_faith
  Layer 2: Holy_Phoenix has capital = Blister_Hill → finds that location

The lore JSON format (from World_lore.json):
  [
    {
      "id":      "lore_holy_nation_overview",
      "type":    "faction",
      "tags":    ["Holy Nation", "Holy_Nation"],
      "content": "A violent human theocracy..."
    },
    ...
  ]

─── USAGE ───────────────────────────────────────────────────────────────────

Run once per campaign (or after lore updates):

    python ss_lore_import.py --lore "path/to/World_lore.json" \
                             --campaign "Default" \
                             --kayak "http://127.0.0.1:5001"

Or call from Python:

    from bridges.sentient_sands.ss_lore_import import import_world_lore
    import_world_lore(
        lore_json_path = "C:/SentientSands/server/templates/World_lore.json",
        campaign_name  = "Default",
        kayak_url      = "http://127.0.0.1:5001",
    )

─── FIELD MAPPING ───────────────────────────────────────────────────────────

lore type "faction"  → category lore_faction
lore type "race"     → category lore_race
lore type "theology" → category lore_theology
lore type "region"   → category lore_region
lore type "global"   → category lore_global

Tags that match known entity names become relational fields (expand on retrieval).
Tags that are just descriptive become keyword fields.
Content becomes text_content (prose — indexed but never expanded).
"""

import argparse
import json
import logging
import os
import re
from typing import Dict, List, Optional

import requests

log = logging.getLogger("kayak.ss_lore_import")

# Lore type → Kayak category name
_TYPE_CATEGORY = {
    "faction":  "lore_faction",
    "race":     "lore_race",
    "theology": "lore_theology",
    "region":   "lore_region",
    "global":   "lore_global",
}

# Tags that are purely classification labels, not world entity names
_META_TAGS = frozenset({
    "global", "all", "world", "general", "universal",
    "lore", "background", "setting",
})


def import_world_lore(
    lore_json_path: str,
    campaign_name:  str,
    kayak_url:      str = "http://127.0.0.1:5001",
    dry_run:        bool = False,
) -> Dict[str, int]:
    """
    Import World_lore.json into Kayak as entities.

    Returns counts: imported, skipped, failed.
    """
    results = {"imported": 0, "skipped": 0, "failed": 0}

    if not os.path.isfile(lore_json_path):
        log.error(f"Lore file not found: {lore_json_path}")
        return results

    try:
        with open(lore_json_path, "r", encoding="utf-8-sig") as f:
            lore_db = json.load(f)
    except Exception as e:
        log.error(f"Cannot read lore file: {e}")
        return results

    if not isinstance(lore_db, list):
        log.error("World_lore.json must be a JSON array")
        return results

    # Ensure campaign loaded
    if not dry_run:
        try:
            r = requests.post(
                f"{kayak_url}/campaign/switch",
                json={"name": campaign_name},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            log.error(f"Cannot connect to Kayak: {e}")
            return results

    log.info(f"Importing {len(lore_db)} lore chunks into campaign '{campaign_name}'")

    for chunk in lore_db:
        chunk_id   = str(chunk.get("id",      "")).strip()
        lore_type  = str(chunk.get("type",    "global")).strip().lower()
        tags       = chunk.get("tags", [])
        content    = str(chunk.get("content", "")).strip()

        if not chunk_id or not content:
            results["skipped"] += 1
            continue

        category    = _TYPE_CATEGORY.get(lore_type, f"lore_{lore_type}")
        folder_name = _to_folder(chunk_id)

        entity_content = _build_lore_entity(
            chunk_id    = chunk_id,
            category    = category,
            lore_type   = lore_type,
            tags        = tags,
            content     = content,
        )

        if dry_run:
            print(f"[DRY RUN] {category}/{folder_name}")
            print(entity_content)
            print()
            results["imported"] += 1
            continue

        try:
            resp = requests.post(
                f"{kayak_url}/write/lore_chunk",
                json={
                    "lore_type":      category,
                    "name":           folder_name,
                    "entity_content": entity_content,
                    "campaign":       campaign_name,
                },
                timeout=10,
            )
            resp.raise_for_status()
            log.info(f"  ✓ {category}/{folder_name}")
            results["imported"] += 1
        except Exception as e:
            log.error(f"  ✗ {chunk_id}: {e}")
            results["failed"] += 1

    log.info(
        f"Lore import complete: "
        f"{results['imported']} imported, "
        f"{results['skipped']} skipped, "
        f"{results['failed']} failed"
    )
    return results


def _build_lore_entity(
    chunk_id:  str,
    category:  str,
    lore_type: str,
    tags:      List[str],
    content:   str,
) -> str:
    """
    Build entity.txt content for a lore chunk.

    Tags that look like entity names become relational fields.
    Tags that are meta labels are skipped.
    Content is text_content (prose, never expanded).
    """
    folder_name  = _to_folder(chunk_id)
    display_name = chunk_id.replace("lore_", "").replace("_", " ").title()

    # Separate tags into relational (world entity names) and meta
    relational_tags: List[str] = []
    for tag in tags:
        tag_clean = tag.strip()
        if not tag_clean:
            continue
        if tag_clean.lower() in _META_TAGS:
            continue
        # Treat as a relational reference (will expand to matching entities)
        relational_tags.append(tag_clean)

    lines = [
        f"Category = {category}",
        f"Name = {folder_name}",
        f"Id =",
        "",
        f"display_name = {display_name}",
        f"lore_type = {lore_type}",
    ]

    # One field per relational tag — the retriever will try to expand each
    # If the tag matches an entity name, that entity gets pulled in
    for i, tag in enumerate(relational_tags):
        field_name = _tag_to_field(tag, lore_type)
        lines.append(f"{field_name} = {tag}")

    # Content as prose field — indexed for search, never expanded
    lines.append(f"$content = {content}")

    return "\n".join(lines)


def _tag_to_field(tag: str, lore_type: str) -> str:
    """
    Map a tag to a field name that gives the retriever useful context.
    e.g. "Holy Nation" in a faction chunk → faction = Holy Nation
    """
    tag_lower = tag.lower()

    if lore_type == "faction":
        return "faction"
    if lore_type == "race":
        return "race"
    if lore_type == "theology":
        return "religion"
    if lore_type == "region":
        return "region"

    # Global: guess from tag
    if any(w in tag_lower for w in ("nation", "empire", "kingdom", "guild", "ninjas", "hive")):
        return "faction"
    if any(w in tag_lower for w in ("human", "shek", "skeleton", "hiver", "greenlander")):
        return "race"
    return "related_to"


# ─── UTILITIES ───────────────────────────────────────────────────────────────

def _to_folder(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", str(name).strip()).strip("_")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Import World_lore.json into Kayak.")
    parser.add_argument("--lore",     required=True, help="Path to World_lore.json")
    parser.add_argument("--campaign", required=True, help="Campaign name in Kayak")
    parser.add_argument("--kayak",    default="http://127.0.0.1:5001", help="Kayak server URL")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without writing")
    args = parser.parse_args()

    import_world_lore(
        lore_json_path = args.lore,
        campaign_name  = args.campaign,
        kayak_url      = args.kayak,
        dry_run        = args.dry_run,
    )
