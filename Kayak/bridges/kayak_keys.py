# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
kayak_keys.py — stable key contract for bridge operations and common fields.

This module is intentionally small and runtime-safe. Bridges and server-side
adapters can import these constants instead of repeating string literals.
"""


class Ops:
    """Operation keys used by compatibility adapters."""

    LIST_CHARACTERS = "LIST_CHARACTERS"
    READ_PROFILE = "READ_PROFILE"
    WRITE_PROFILE = "WRITE_PROFILE"


class Fields:
    """Common normalized entity field keys."""

    ID = "id"
    PERSISTENT_ID = "persistent_id"
    RUNTIME_ID = "runtime_id"
    DISPLAY_NAME = "display_name"
    RACE = "race"
    SEX = "sex"
    FACTION = "faction"
    ORIGIN_FACTION = "origin_faction"
    ROLE = "role"
    RELATION = "relation"
    PERSONALITY = "personality"
    BACKSTORY = "backstory"
    SPEECH_QUIRKS = "speech_quirks"
    KNOWS_ABOUT = "knows_about"

