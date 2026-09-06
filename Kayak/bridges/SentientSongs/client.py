# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

from .SentientSongs import MODULE_DIR, ensure_sentient_songs_layout, load_sentient_songs_config


class SentientSongsClient:
    """Controller client used by Kayak and StandalonePlay."""

    def __init__(
        self,
        controller_name: str,
        auto_start: bool = True,
        config_path: Path | str | None = None,
        request_timeout: float = 2.0,
    ):
        self.controller_name = str(controller_name or "").strip().lower() or "controller"
        self.auto_start = auto_start
        self.config_path = ensure_sentient_songs_layout(config_path)
        self.request_timeout = request_timeout
        self._session = requests.Session()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self.config = load_sentient_songs_config(self.config_path)

    def reload_config(self):
        self.config = load_sentient_songs_config(self.config_path)
        return self.config

    def _request(self, method: str, path: str, payload: Optional[dict] = None):
        url = f"{self.config.base_url}{path}"
        return self._session.request(method, url, json=payload, timeout=self.request_timeout)

    def is_alive(self) -> bool:
        self.reload_config()
        try:
            response = self._request("GET", "/status")
            return response.ok
        except Exception:
            return False

    def _start_process(self) -> None:
        script_path = MODULE_DIR / "SentientSongs.py"
        args = [sys.executable, str(script_path), "--serve"]
        kwargs = {
            "cwd": str(MODULE_DIR),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)

    def ensure_service(self) -> Tuple[bool, str]:
        self.reload_config()
        if self.is_alive():
            return True, ""
        if not self.auto_start:
            return False, "[MUSIC] SentientSongs service is not running."
        try:
            self._start_process()
        except Exception as exc:
            return False, f"[MUSIC] Failed to start SentientSongs: {exc}"
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.is_alive():
                return True, ""
            time.sleep(0.25)
        return False, "[MUSIC] SentientSongs did not start in time."

    def heartbeat_once(self) -> Tuple[bool, str]:
        ok, message = self.ensure_service()
        if not ok:
            return False, message
        try:
            response = self._request("POST", "/heartbeat", {"controller": self.controller_name})
            data = response.json()
            if response.ok and data.get("ok"):
                return True, ""
            return False, str(data.get("message") or "[MUSIC] Heartbeat failed.")
        except Exception as exc:
            return False, f"[MUSIC] Heartbeat failed: {exc}"

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.config.heartbeat_interval):
            self.reload_config()
            self.heartbeat_once()

    def start_heartbeat(self) -> Tuple[bool, str]:
        ok, message = self.heartbeat_once()
        if not ok:
            return False, message
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return True, ""
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"SentientSongsHeartbeat-{self.controller_name}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return True, ""

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def send_command(self, command_text: str) -> str:
        ok, message = self.ensure_service()
        if not ok:
            return message
        try:
            response = self._request(
                "POST",
                "/command",
                {"controller": self.controller_name, "command": command_text},
            )
            data = response.json()
            if response.ok and data.get("ok"):
                return str(data.get("text") or "")
            return str(data.get("message") or "[MUSIC] Command failed.")
        except Exception as exc:
            return f"[MUSIC] Failed to reach SentientSongs: {exc}"

    def status_text(self) -> str:
        ok, message = self.ensure_service()
        if not ok:
            return message
        try:
            response = self._request("GET", "/status")
            data = response.json()
            if response.ok and data.get("ok"):
                return str(data.get("status") or "[MUSIC] SentientSongs is running.")
            return str(data.get("message") or "[MUSIC] Failed to get status.")
        except Exception as exc:
            return f"[MUSIC] Failed to get SentientSongs status: {exc}"

    def shutdown_service(self) -> str:
        if self.controller_name != "standalone":
            return "[MUSIC] Shutdown is only available to standalone."
        ok, message = self.ensure_service()
        if not ok:
            return message
        try:
            response = self._request("POST", "/shutdown", {"controller": self.controller_name})
            data = response.json()
            if response.ok and data.get("ok"):
                return str(data.get("message") or "SentientSongs shutting down")
            return str(data.get("message") or "[MUSIC] Shutdown failed.")
        except Exception as exc:
            return f"[MUSIC] Failed to shut down SentientSongs: {exc}"
