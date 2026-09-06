# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""
Kayak campaign manager.

Responsibilities:
  - Create new campaigns (copy Template/ → Campaigns/<name>/)
  - Load / switch campaigns (triggers full reindex)
  - Expose the active indexer and prompt builder
  - Thread-safe (callers may switch campaigns from multiple threads)

Template/ is NEVER accessed at runtime.
Runtime only ever reads from Campaigns/<active_name>/.
"""

import os
import shutil
import threading
from typing import Optional

from .config        import load_config
from .indexer       import Indexer
from .prompt_builder import PromptBuilder


class CampaignManager:
    def __init__(self, kayak_root: str):
        self.kayak_root      = kayak_root
        self.kayakdb_root    = os.path.join(kayak_root, "KayakDB")
        self.template_dir    = os.path.join(self.kayakdb_root, "Template")
        self.campaigns_dir   = os.path.join(self.kayakdb_root, "Campaigns")
        self.config_dir      = os.path.join(kayak_root, "config")

        os.makedirs(self.campaigns_dir, exist_ok=True)

        self._config: dict               = load_config(self.config_dir)
        self._lock                       = threading.RLock()
        self._active_name:  Optional[str]           = None
        self._active_path:  Optional[str]           = None
        self._indexer:      Optional[Indexer]       = None
        self._prompt_builder: Optional[PromptBuilder] = None

    # ─── CAMPAIGN LIFECYCLE ───────────────────────────────────────────────

    def list_campaigns(self):
        return sorted(
            d for d in os.listdir(self.campaigns_dir)
            if os.path.isdir(os.path.join(self.campaigns_dir, d))
        )

    def campaign_exists(self, name: str) -> bool:
        try:
            return os.path.isdir(self._campaign_path(name))
        except ValueError:
            return False

    def create_campaign(self, name: str) -> str:
        """
        Copy Template/ into Campaigns/<name>/.
        Returns the new campaign directory path.
        Raises ValueError if it already exists.
        """
        safe_name = self._validate_campaign_name(name)
        dest = self._campaign_path(safe_name)
        if os.path.exists(dest):
            raise ValueError(f"Campaign '{safe_name}' already exists.")
        if not os.path.isdir(self.template_dir):
            raise RuntimeError(f"Template directory not found: {self.template_dir}")
        shutil.copytree(self.template_dir, dest)
        return dest

    def load_campaign(self, name: str):
        """
        Load the named campaign as the active campaign.
        Rebuilds the in-memory index from scratch.
        Thread-safe.
        """
        safe_name = self._validate_campaign_name(name)
        path = self._campaign_path(safe_name)
        if not os.path.isdir(path):
            raise ValueError(f"Campaign '{safe_name}' does not exist. Create it first.")

        with self._lock:
            categories_path = os.path.join(path, "categories")
            indexer = Indexer(categories_path, self._config)
            indexer.build()

            self._active_name    = safe_name
            self._active_path    = path
            self._indexer        = indexer
            self._prompt_builder = PromptBuilder(path)

    def reload_index(self):
        """Force a full reindex of the currently active campaign."""
        if not self._active_name:
            raise RuntimeError("No active campaign to reload.")
        self.load_campaign(self._active_name)

    def reload_config(self):
        """Re-read core_config.txt without restarting the server."""
        self._config = load_config(self.config_dir)
        if self._indexer:
            self._indexer.config = self._config
        return self._config

    def ensure_default_campaign(self):
        """
        Load the default campaign, creating it from Template if it doesn't exist.
        Called at server startup.
        """
        default = self._config.get("default_campaign", "Default")
        if not self.campaign_exists(default):
            self.create_campaign(default)
        self.load_campaign(default)

    def _validate_campaign_name(self, name: str) -> str:
        safe = str(name or "").strip()
        if not safe:
            raise ValueError("Campaign name is required.")
        if safe in (".", ".."):
            raise ValueError("Invalid campaign name.")
        if "/" in safe or "\\" in safe or "\x00" in safe:
            raise ValueError("Campaign name cannot contain path separators.")
        return safe

    def _campaign_path(self, name: str) -> str:
        safe_name = self._validate_campaign_name(name)
        root = os.path.abspath(self.campaigns_dir)
        path = os.path.abspath(os.path.join(root, safe_name))
        if os.path.commonpath([root, path]) != root:
            raise ValueError("Campaign path escapes Campaigns directory.")
        return path

    # ─── ACCESSORS ───────────────────────────────────────────────────────

    @property
    def db_root(self) -> str:
        """Root of the KayakDB folder — used by server endpoints."""
        return self.kayakdb_root

    @property
    def config(self) -> dict:
        return self._config

    @property
    def active_name(self) -> Optional[str]:
        return self._active_name

    @property
    def active_path(self) -> Optional[str]:
        return self._active_path

    @property
    def indexer(self) -> Optional[Indexer]:
        return self._indexer

    @property
    def prompt_builder(self) -> Optional[PromptBuilder]:
        return self._prompt_builder
