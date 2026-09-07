# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak prompt builder.

KEY DESIGN: the bridge sets the PromptPolicy, the core executes it blindly.
The core never decides which files to include — that's the bridge's job.

PromptPolicy declares:
  - which aux files load for the target NPC
  - which aux files load for world context entities
  - whether to include dialogue
  - how many dialogue lines
  - optional suffix appended after PLAYER_MESSAGE (mode instructions etc.)

Prompt assembly order:
  1. Mandatory text — files from mandatory/<PromptType>/, numeric order
  2. WORLD_CONTEXT — retrieved entities (+ any world_aux_files)
  3. TARGET_NPC   — target entity (+ target_aux_files)
  4. DIALOGUE     — if include_dialogue and dialogue exists
  5. PLAYER_MESSAGE (+ player_message_suffix if set)

Structured sections use "--- LABEL" for world/target/dialogue/player blocks.
Mandatory files are inserted raw so their filenames/labels do not leak into the prompt.

INDEX vs PROMPT-TIME READS:
  entity.txt plus indexed retrieval metadata are precompiled.
  Aux files like stats.txt/dialogue.txt are read fresh here at prompt-time.
  No reindex is needed when stats.txt or dialogue.txt change.
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .indexer import Entity
from .token_resolver import TokenResolver, TokenResolverContext

SEP = "---"
_JUDGMENT_TAG_RE = re.compile(r"\[\s*JUDGMENT\s*:\s*[^\]]+\]", re.IGNORECASE)


# ─── PROMPT POLICY ───────────────────────────────────────────────────────────

@dataclass
class PromptPolicy:
    """
    Declares what goes into the prompt.
    The bridge creates this; the core executes it without making any decisions.

    Pre-built policies are in bridges/sentient_sands/bridge.py.
    """
    prompt_type: str = "Chat"

    # Aux files to load for the target NPC alongside entity.txt
    # e.g. ["stats.txt"] — dialogue handled separately by include_dialogue
    target_aux_files: List[str] = field(default_factory=list)

    # Whether to include a DIALOGUE block for the target NPC
    include_dialogue: bool = True

    # Max lines from dialogue.txt to inject
    dialogue_keep_lines: int = 30

    # Aux files to load for world context entities (usually empty)
    world_aux_files: List[str] = field(default_factory=list)

    # Optional text appended to the PLAYER_MESSAGE section.
    # Used for mode instructions (yell/whisper/talk) without touching the core.
    player_message_suffix: str = ""

    # Section label for the target NPC block
    target_label: str = "TARGET_NPC"


# Pre-built standard policies. Bridges can use these or create their own.
POLICY_CHAT = PromptPolicy(
    prompt_type          = "Chat",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
)

POLICY_CHAT_ANIMAL = PromptPolicy(
    prompt_type          = "ChatAnimal",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
)

POLICY_CHAT_MACHINE = PromptPolicy(
    prompt_type          = "ChatMachine",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
)

POLICY_CHAT_FERAL = PromptPolicy(
    prompt_type          = "ChatFeral",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
)

POLICY_CHAT_SAPIENT = PromptPolicy(
    prompt_type          = "ChatSapient",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
)

POLICY_CHAT_WHISPER = PromptPolicy(
    prompt_type          = "Chat",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 30,
    # Token prompt architecture: whisper/yell instructions now belong in mandatory
    # prompt files via <mode_guidance>, not as hidden PLAYER_MESSAGE suffixes.
    player_message_suffix = "",
)

POLICY_CHAT_YELL = PromptPolicy(
    prompt_type          = "Chat",
    target_aux_files     = ["stats.txt"],
    include_dialogue     = True,
    dialogue_keep_lines  = 20,
    # Token prompt architecture: whisper/yell instructions now belong in mandatory
    # prompt files via <mode_guidance>, not as hidden PLAYER_MESSAGE suffixes.
    player_message_suffix = "",
)

POLICY_LOREMASTER = PromptPolicy(
    prompt_type          = "Loremaster",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_BIOGRAPHY = PromptPolicy(
    prompt_type          = "Biography",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_BIOGRAPHY_ANIMAL = PromptPolicy(
    prompt_type          = "BiographyAnimal",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_BIOGRAPHY_MACHINE = PromptPolicy(
    prompt_type          = "BiographyMachine",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_BIOGRAPHY_FERAL = PromptPolicy(
    prompt_type          = "BiographyFeral",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_BIOGRAPHY_SAPIENT = PromptPolicy(
    prompt_type          = "BiographySapient",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

POLICY_CHAT_RADIANT = PromptPolicy(
    prompt_type          = "ChatRadiant",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)

# Speak policy: used by /k_speak so the LLM echoes a line verbatim.
# No world context, no dialogue history, no target entity, no action tags.
# The server builds the prompt directly via /prompt/speak.
POLICY_SPEAK = PromptPolicy(
    prompt_type          = "Speak",
    target_aux_files     = [],
    include_dialogue     = False,
    dialogue_keep_lines  = 0,
)


# ─── PROMPT BUILDER ──────────────────────────────────────────────────────────

class PromptBuilder:
    def __init__(self, campaign_root: str):
        self.campaign_root = campaign_root
        self.template_root = os.path.normpath(os.path.join(self.campaign_root, "..", "..", "Template"))

    # ── TOKEN EXPANSION ──────────────────────────────────────────────────

    def expand_prompt_text(self, text: str, token_context: Optional[TokenResolverContext] = None, indexer=None) -> str:
        """Expand whitelisted <...> prompt tokens in user-authored prompt text."""
        if not token_context or not text or "<" not in text:
            return text or ""
        resolver = TokenResolver(indexer=indexer, prompt_builder=self)
        return resolver.expand_text(text, token_context)

    def expand_prompt_texts(self, texts: List[str], token_context: Optional[TokenResolverContext] = None, indexer=None) -> List[str]:
        return [self.expand_prompt_text(t, token_context, indexer) for t in (texts or [])]

    # ── MANDATORY ────────────────────────────────────────────────────────

    @staticmethod
    def _safe_species_folder(race: str) -> str:
        text = str(race or "").strip()
        if not text:
            return ""
        text = re.sub(r"[^\w\s\-]", "", text)
        return re.sub(r"\s+", "_", text).strip("_")

    @staticmethod
    def _species_prompt_group(prompt_type: str) -> str:
        text = str(prompt_type or "").strip()
        for suffix in ("Animal", "Machine", "Feral", "Sapient"):
            if text.endswith(suffix):
                text = text[:-len(suffix)]
                break
        return text

    @staticmethod
    def _species_bucket(persona_category: str) -> str:
        category = str(persona_category or "").strip().lower()
        mapping = {
            "animal": "animals",
            "machine": "machines",
            "feral": "ferals",
            "sapient": "sapients",
        }
        return mapping.get(category, "")

    def _load_numbered_texts(self, directory: str) -> List[str]:
        if not os.path.isdir(directory):
            return []
        numbered: List[Tuple[int, str]] = []
        for fname in os.listdir(directory):
            if fname.startswith("IGN_"):
                continue
            # Только .txt. Префикса мало: редакторский бэкап "1_core.txt.bak"
            # тоже начинается с "1_" и грузился бы наравне с оригиналом,
            # молча удваивая промпт.
            if not fname.lower().endswith(".txt"):
                continue
            m = re.match(r'^(\d+)[_\-]', fname)
            if m:
                numbered.append((int(m.group(1)), fname))
        numbered.sort(key=lambda x: x[0])
        result = []
        for _, fname in numbered:
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        result.append(content)
            except OSError:
                pass
        return result

    def load_mandatory(self, prompt_type: str) -> List[str]:
        """Load mandatory files for prompt_type in numeric prefix order."""
        result = self._load_numbered_texts(os.path.join(self.campaign_root, "mandatory", prompt_type))
        if result:
            return result
        result = self._load_numbered_texts(os.path.join(self.template_root, "mandatory", prompt_type))
        if result:
            return result
        if str(prompt_type or "").endswith("Sapient"):
            base_prompt_type = prompt_type[:-len("Sapient")]
            result = self._load_numbered_texts(os.path.join(self.campaign_root, "mandatory", base_prompt_type))
            if result:
                return result
            return self._load_numbered_texts(os.path.join(self.template_root, "mandatory", base_prompt_type))
        return []

    def load_prompt_sections(self, prompt_type: str, race: str = "", persona_category: str = "") -> List[str]:
        """Load mandatory text plus optional species overlays for one prompt family."""
        sections = list(self.load_mandatory(prompt_type))
        sections.extend(self.load_species_overlays(prompt_type, race, persona_category))
        return sections

    def load_species_overlays(self, prompt_type: str, race: str, persona_category: str = "") -> List[str]:
        """Load optional per-species prompt overlays for a race and prompt type."""
        species = self._safe_species_folder(race)
        if not species:
            return []
        prompt_group = self._species_prompt_group(prompt_type)
        bucket = self._species_bucket(persona_category)
        search_dirs = []
        if bucket:
            search_dirs.extend([
                os.path.join(self.campaign_root, "species", bucket, species, prompt_group),
                os.path.join(self.template_root, "species", bucket, species, prompt_group),
            ])
        # Backward-compatible flat layout fallback.
        search_dirs.extend([
            os.path.join(self.campaign_root, "species", species, prompt_group),
            os.path.join(self.template_root, "species", species, prompt_group),
        ])
        for directory in search_dirs:
            result = self._load_numbered_texts(directory)
            if result:
                return result
        return []

    # ── ENTITY RENDERING ─────────────────────────────────────────────────

    def render_entity(self, entity: Entity, aux_files: Optional[List[str]] = None) -> str:
        """
        Render an entity as a clean prompt block.
        aux_files are read fresh from disk here (no reindex needed).
        """
        lines = [
            f"Category: {entity.category}",
            f"Name: {entity.display_name}",
        ]
        if entity.best_id:
            lines.append(f"Id: {entity.best_id}")

        for k, v in entity.fields.items():
            if k.lower() in ("weight",):
                continue
            # Strip $ prose marker — LLM sees "personality: ..." not "$personality: ..."
            display_key = k.lstrip("$")
            lines.append(f"{display_key}: {v}")

        if entity.free_text.strip():
            lines.append("")
            lines.append(entity.free_text.strip())

        if aux_files:
            for aux_name in aux_files:
                content = _read_aux(entity.path, aux_name)
                if content:
                    label = os.path.splitext(aux_name)[0].upper()
                    lines.append(f"\n[{label}]")
                    lines.append(content)

        return "\n".join(lines)

    # ── DIALOGUE ─────────────────────────────────────────────────────────

    def load_dialogue(self, entity: Entity, keep_lines: int = 30) -> str:
        """Return the most recent lines of an entity's dialogue.txt."""
        raw = _tail(os.path.join(entity.path, "dialogue.txt"), keep_lines)
        if not raw:
            return ""
        clean_lines = []
        for line in raw.splitlines():
            line = _JUDGMENT_TAG_RE.sub("", line)
            line = re.sub(r"\s{2,}", " ", line).strip()
            if line:
                clean_lines.append(line)
        return "\n".join(clean_lines)

    def get_dialogue(self, entity: Entity, keep_lines: int = 30) -> List[str]:
        """Compatibility helper for callers that expect dialogue as a list of lines."""
        dlg = self.load_dialogue(entity, keep_lines)
        return [line for line in dlg.splitlines() if line.strip()] if dlg else []

    def save_dialogue_turn(
        self,
        entity:     Entity,
        player_line: str,
        npc_line:    Optional[str],
        keep_lines:  int = 30,
    ):
        """
        Append player + NPC lines to dialogue.txt.
        Trim to keep_lines; archive overflow to IGN_dialogue_backup/archive.txt.

        *** Call AFTER the LLM responds — never before. ***
        """
        path = os.path.join(entity.path, "dialogue.txt")
        os.makedirs(entity.path, exist_ok=True)

        lines: List[str] = []
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]

        if player_line:
            lines.append(f"Player: {player_line}")
        if npc_line:
            lines.append(f"NPC: {npc_line}")

        if len(lines) > keep_lines:
            overflow = lines[:-keep_lines]
            lines    = lines[-keep_lines:]
            bak_dir  = os.path.join(entity.path, "IGN_dialogue_backup")
            os.makedirs(bak_dir, exist_ok=True)
            with open(os.path.join(bak_dir, "archive.txt"), "a", encoding="utf-8") as bf:
                bf.writelines(l + "\n" for l in overflow)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def replace_dialogue(self, entity: Entity, lines: List[str], keep_lines: int = 30):
        """Replace dialogue.txt with a normalized set of lines."""
        path = os.path.join(entity.path, "dialogue.txt")
        os.makedirs(entity.path, exist_ok=True)
        clean_lines = [str(line).rstrip("\n") for line in (lines or []) if str(line).strip()]
        if keep_lines and len(clean_lines) > keep_lines:
            clean_lines = clean_lines[-keep_lines:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(clean_lines))

    def update_stats(self, entity: Entity, stats_content: str):
        """Overwrite stats.txt. Read at prompt-time — no reindex needed."""
        path = os.path.join(entity.path, "stats.txt")
        os.makedirs(entity.path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(stats_content)

    # ── FINAL ASSEMBLY ───────────────────────────────────────────────────

    def assemble(
        self,
        policy:         PromptPolicy,
        world_entities: List[Entity],
        target_entity:  Optional[Entity],
        player_message: str,
        species_race:   Optional[str] = None,
        species_category: Optional[str] = None,
        token_context: Optional[TokenResolverContext] = None,
        indexer = None,
    ) -> str:
        """
        Assemble the final prompt following the bridge-supplied policy.
        The core makes zero inclusion decisions here.
        """
        sections: List[str] = []

        # Build legacy core payloads as DATA for explicit tokens only.
        # They must not be appended automatically. Mandatory prompts decide
        # whether/where these blocks appear via:
        #   <world_context>
        #   <target_npc_context>
        #   <dialogue_lines_qty N>
        if token_context is None:
            token_context = TokenResolverContext(campaign_root=self.campaign_root)
        world_blocks = [
            self.render_entity(ent, aux_files=policy.world_aux_files or None)
            for ent in (world_entities or [])
            if ent is not None
        ]
        token_context.world_context = "\n\n---\n\n".join(b for b in world_blocks if b.strip())
        token_context.target_npc_context = (
            self.render_entity(target_entity, aux_files=policy.target_aux_files or None)
            if target_entity else ""
        )
        if target_entity is not None and token_context.target_entity is None:
            token_context.target_entity = target_entity
        if not token_context.player_message:
            token_context.player_message = player_message or ""

        # 1. Mandatory. These are now the sole place where world/target/dialogue
        # sections are inserted. No hidden WORLD_CONTEXT/TARGET_NPC/DIALOGUE
        # fallback is appended below.
        for content in self.load_mandatory(policy.prompt_type):
            sections.append(self.expand_prompt_text(content, token_context, indexer))

        # 1b. Optional species overlays
        effective_race = species_race
        if not effective_race and target_entity:
            effective_race = target_entity.fields.get("race", "")
        effective_category = species_category
        if not effective_category and target_entity:
            effective_category = target_entity.fields.get("persona_category", "")
        for content in self.load_species_overlays(policy.prompt_type, effective_race or "", effective_category or ""):
            sections.append(self.expand_prompt_text(content, token_context, indexer))

        # 2. Player message (+ optional mode suffix from bridge)
        # The player message remains system-side for now because it is the live
        # game input. Prompt context around it is controlled by mandatory tokens.
        pm = player_message
        if policy.player_message_suffix:
            pm = pm + policy.player_message_suffix
        sections.append(f"{SEP} PLAYER_MESSAGE\n{pm}")

        prompt = "\n\n".join(sections)
        self._log_prompt(policy.prompt_type, prompt)
        return prompt

    def assemble_radiant(
        self,
        speakers: List[dict],
        player_name: str = "Drifter",
        world_lore: str = "",
        events: str = "",
        recent_dialogue: str = "",
        token_context: Optional[TokenResolverContext] = None,
        indexer = None,
    ) -> str:
        """Assemble a category-aware radiant prompt with per-speaker guidance."""
        sections: List[str] = []

        for content in self.load_mandatory(POLICY_CHAT_RADIANT.prompt_type):
            sections.append(self.expand_prompt_text(content, token_context, indexer))

        if world_lore:
            sections.append(f"{SEP} WORLD_LORE\n{world_lore}")
        if events:
            sections.append(f"{SEP} EVENTS\n{events}")

        for speaker in speakers or []:
            name = str((speaker or {}).get("name") or "Unknown").strip() or "Unknown"
            speaker_id = str((speaker or {}).get("id") or "0").strip() or "0"
            gender = str((speaker or {}).get("gender") or "Unknown").strip() or "Unknown"
            race = str((speaker or {}).get("race") or "Unknown").strip() or "Unknown"
            faction = str((speaker or {}).get("faction") or "Unknown").strip() or "Unknown"
            health = str((speaker or {}).get("health") or "Healthy").strip() or "Healthy"
            gear = str((speaker or {}).get("gear") or "nothing notable").strip() or "nothing notable"
            personality = str((speaker or {}).get("personality") or "A traveler.").strip() or "A traveler."
            persona_category = str((speaker or {}).get("persona_category") or "sapient").strip().lower() or "sapient"

            guidance_prompt = {
                "animal": "RadiantSpeakerAnimal",
                "machine": "RadiantSpeakerMachine",
                "feral": "RadiantSpeakerFeral",
            }.get(persona_category, "RadiantSpeakerSapient")

            speaker_lines = [
                f"Name|ID: {name}|{speaker_id}",
                f"Category: {persona_category}",
                f"Identity: {gender} {race} of {faction}",
                f"Health: {health}",
                f"Gear: {gear}",
                f"Personality: {personality}",
            ]
            guidance_sections = self.expand_prompt_texts(
                self.load_prompt_sections(guidance_prompt, race, persona_category),
                token_context,
                indexer,
            )
            if guidance_sections:
                speaker_lines.append("Guidance:")
                speaker_lines.extend(guidance_sections)
            sections.append(f"{SEP} SPEAKER\n" + "\n".join(speaker_lines))

        if recent_dialogue:
            sections.append(f"{SEP} RECENT_LOCAL_DIALOGUE\n{recent_dialogue}")

        # PLAYER_MESSAGE may remain system-side for now, but it must be neutral.
        # Output contract / style should live in ChatRadiant mandatory prompts.
        sections.append(f"{SEP} PLAYER_MESSAGE\nGenerate radiant interaction near {player_name}.")

        prompt = "\n\n".join(section for section in sections if section.strip())
        self._log_prompt(POLICY_CHAT_RADIANT.prompt_type, prompt)
        return prompt

    # ── Prompt log ───────────────────────────────────────────────────────────

    def _log_prompt(self, prompt_type: str, prompt: str, keep: int = 20):
        """
        Append the assembled prompt to logs/<prompt_type>.log in the campaign
        root, keeping only the last `keep` entries. Entries are separated by
        a fixed divider so the file is human-readable.
        """
        try:
            log_dir = os.path.join(self.campaign_root, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f"{prompt_type.lower()}.log")

            DIVIDER = "\n" + "=" * 80 + "\n"

            # Read existing entries
            existing = ""
            if os.path.isfile(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()

            entries = [e for e in existing.split(DIVIDER) if e.strip()]
            entries.append(prompt.strip())

            # Trim to keep
            if len(entries) > keep:
                entries = entries[-keep:]

            with open(log_path, "w", encoding="utf-8") as f:
                f.write(DIVIDER.join(entries))
        except Exception:
            pass  # logging must never break the prompt pipeline


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _read_aux(entity_path: str, filename: str) -> str:
    """Read an auxiliary file from an entity folder. Fresh read every time."""
    try:
        with open(os.path.join(entity_path, filename), "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _tail(path: str, keep_lines: int = 0) -> str:
    """Read a file; return last keep_lines lines (0 = all)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
    except OSError:
        return ""
    if keep_lines and len(lines) > keep_lines:
        lines = lines[-keep_lines:]
    return "\n".join(lines)
