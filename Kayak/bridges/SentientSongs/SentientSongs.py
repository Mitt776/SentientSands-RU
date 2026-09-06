# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

from __future__ import annotations

import argparse
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
from werkzeug.serving import make_server


MODULE_DIR = Path(__file__).resolve().parent
SONGS_DB_DIR = MODULE_DIR / "SongsDB"
PLAYLISTS_DIR = MODULE_DIR / "playlists"
CONFIG_PATH = MODULE_DIR / "config.txt"
SUPPORTED_EXTENSIONS = {".mp3", ".wav"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4999
DEFAULT_HEARTBEAT_TIMEOUT = 30
DEFAULT_HEARTBEAT_INTERVAL = 10
DEFAULT_VOLUME = 70
DEFAULT_SHUFFLE = False
DEFAULT_LOOP = "off"


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"\.[A-Za-z0-9]+$", "", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.casefold().strip()


def _clean_command_text(value: str) -> str:
    return str(value or "").strip()


def _bool_from_text(value: str, default: bool = False) -> bool:
    text = _normalize_text(value)
    if text in {"1", "true", "on", "yes"}:
        return True
    if text in {"0", "false", "off", "no"}:
        return False
    return default


def _safe_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class SentientSongsConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT
    heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL
    default_volume: int = DEFAULT_VOLUME
    shuffle: bool = DEFAULT_SHUFFLE
    loop: str = DEFAULT_LOOP

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def ensure_sentient_songs_layout(config_path: Path | str | None = None) -> Path:
    cfg_path = Path(config_path or CONFIG_PATH).resolve()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    SONGS_DB_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(
            "# SentientSongs configuration\n"
            "host = 127.0.0.1\n"
            "port = 4999\n"
            "heartbeat_timeout = 30\n"
            "heartbeat_interval = 10\n"
            "default_volume = 70\n"
            "shuffle = off\n"
            "loop = off\n",
            encoding="utf-8",
        )
    return cfg_path


def load_sentient_songs_config(config_path: Path | str | None = None) -> SentientSongsConfig:
    cfg_path = ensure_sentient_songs_layout(config_path)
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    heartbeat_timeout = DEFAULT_HEARTBEAT_TIMEOUT
    heartbeat_interval = DEFAULT_HEARTBEAT_INTERVAL
    default_volume = DEFAULT_VOLUME
    shuffle = DEFAULT_SHUFFLE
    loop = DEFAULT_LOOP

    for raw_line in cfg_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        key_norm = _normalize_text(key)
        if key_norm == "host":
            host = value or DEFAULT_HOST
        elif key_norm == "port":
            port = _safe_int(value, DEFAULT_PORT, 1, 65535)
        elif key_norm == "heartbeat timeout":
            heartbeat_timeout = _safe_int(value, DEFAULT_HEARTBEAT_TIMEOUT, 5, 3600)
        elif key_norm == "heartbeat interval":
            heartbeat_interval = _safe_int(value, DEFAULT_HEARTBEAT_INTERVAL, 1, 600)
        elif key_norm == "default volume":
            default_volume = _safe_int(value, DEFAULT_VOLUME, 0, 100)
        elif key_norm == "shuffle":
            shuffle = _bool_from_text(value, DEFAULT_SHUFFLE)
        elif key_norm == "loop":
            loop_candidate = _normalize_text(value)
            if loop_candidate in {"off", "track", "folder"}:
                loop = loop_candidate

    if heartbeat_interval >= heartbeat_timeout:
        heartbeat_interval = max(1, heartbeat_timeout // 2)

    return SentientSongsConfig(
        host=host,
        port=port,
        heartbeat_timeout=heartbeat_timeout,
        heartbeat_interval=heartbeat_interval,
        default_volume=default_volume,
        shuffle=shuffle,
        loop=loop,
    )


@dataclass(frozen=True)
class SongEntry:
    name: str
    relative_path: str
    folder: str
    extension: str
    path: Path
    sort_key: str


class SentientSongsManager:
    """Lightweight folder-driven music manager for Kayak and standalone use."""

    def __init__(
        self,
        songs_dir: Path | str | None = None,
        playlists_dir: Path | str | None = None,
        config_path: Path | str | None = None,
    ):
        self.songs_dir = Path(songs_dir or SONGS_DB_DIR).resolve()
        self.playlists_dir = Path(playlists_dir or PLAYLISTS_DIR).resolve()
        self.config_path = Path(config_path or CONFIG_PATH).resolve()
        self._lock = threading.RLock()
        self._pygame = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._songs: List[SongEntry] = []
        self._song_index: Dict[str, List[SongEntry]] = {}
        self._folder_index: Dict[str, List[SongEntry]] = {}
        self._playlist_index: Dict[str, List[str]] = {}
        self._playlist_names: Dict[str, str] = {}
        self.queue: List[SongEntry] = []
        self.queue_label = ""
        self.queue_mode = "idle"
        self.current_index = -1
        self.state = "stopped"
        self.volume_percent = DEFAULT_VOLUME
        self.shuffle = DEFAULT_SHUFFLE
        self.loop_mode = DEFAULT_LOOP
        self._last_play_started_at = 0.0
        self._ensure_directories()
        self._load_config()
        self.reload_index()

    def _ensure_directories(self) -> None:
        ensure_sentient_songs_layout(self.config_path)
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> None:
        config = load_sentient_songs_config(self.config_path)
        self.volume_percent = config.default_volume
        self.shuffle = config.shuffle
        self.loop_mode = config.loop

    def _ensure_audio(self) -> Tuple[bool, str]:
        if self._pygame is not None:
            return True, ""
        try:
            import pygame  # type: ignore
        except Exception as exc:
            return False, f"[MUSIC] pygame is not installed: {exc}"
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.set_volume(self.volume_percent / 100.0)
        except Exception as exc:
            return False, f"[MUSIC] Audio init failed: {exc}"
        self._pygame = pygame
        self._ensure_monitor()
        return True, ""

    def _ensure_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="SentientSongsMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.is_set():
            time.sleep(0.5)
            with self._lock:
                if self.state != "playing" or not self.queue or self.current_index < 0 or self._pygame is None:
                    continue
                try:
                    is_busy = bool(self._pygame.mixer.music.get_busy())
                except Exception:
                    continue
                if is_busy:
                    continue
                if time.monotonic() - self._last_play_started_at < 0.75:
                    continue
            self._advance_after_track_end()

    def _advance_after_track_end(self) -> None:
        with self._lock:
            if not self.queue:
                self.state = "stopped"
                return
            if self.loop_mode == "track":
                self._play_index(self.current_index)
                return
            next_index = self.current_index + 1
            if next_index < len(self.queue):
                self._play_index(next_index)
                return
            if self.loop_mode == "folder":
                self._play_index(0)
                return
            self.state = "stopped"

    def _add_song_alias(self, alias: str, song: SongEntry) -> None:
        key = _normalize_text(alias)
        if not key:
            return
        bucket = self._song_index.setdefault(key, [])
        if song not in bucket:
            bucket.append(song)

    def _add_folder_alias(self, alias: str, song: SongEntry) -> None:
        key = _normalize_text(alias)
        if not key:
            return
        bucket = self._folder_index.setdefault(key, [])
        if song not in bucket:
            bucket.append(song)

    def reload_index(self) -> str:
        with self._lock:
            self._load_config()
            self._songs = []
            self._song_index = {}
            self._folder_index = {}
            self._playlist_index = {}
            self._playlist_names = {}

            for path in sorted(self.songs_dir.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                rel = path.relative_to(self.songs_dir).as_posix()
                folder = path.parent.relative_to(self.songs_dir).as_posix() if path.parent != self.songs_dir else "root"
                song = SongEntry(
                    name=path.stem,
                    relative_path=rel,
                    folder=folder,
                    extension=path.suffix.lower(),
                    path=path,
                    sort_key=rel.casefold(),
                )
                self._songs.append(song)
                self._add_song_alias(song.name, song)
                self._add_song_alias(song.relative_path, song)
                self._add_song_alias(path.stem + path.suffix.lower(), song)

                rel_parent = path.parent.relative_to(self.songs_dir)
                parts = rel_parent.parts
                if not parts:
                    self._add_folder_alias("root", song)
                else:
                    for idx in range(1, len(parts) + 1):
                        segment = "/".join(parts[:idx])
                        self._add_folder_alias(segment, song)
                    self._add_folder_alias(parts[-1], song)

            for playlist_path in sorted(self.playlists_dir.rglob("*.txt")):
                stem = playlist_path.stem.casefold()
                if stem.startswith("readme") or stem.startswith("ph_"):
                    continue
                lines = []
                for raw_line in playlist_path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    lines.append(line)
                key = _normalize_text(playlist_path.stem)
                self._playlist_index[key] = lines
                self._playlist_names[key] = playlist_path.stem

            self._songs.sort(key=lambda item: item.sort_key)

            return (
                f"[MUSIC] Reloaded index: {len(self._songs)} songs, "
                f"{len(self._folder_index)} folders, {len(self._playlist_index)} playlists."
            )

    def command_help(self) -> str:
        return (
            "[MUSIC] Commands: /m_play <song>, /m_folder <folder>, /m_playlist <list>, "
            "/m_find <keyword>, /m_pause, /m_resume, /m_stop, /m_next, /m_prev, "
            "/m_vol <0-100>, /m_shuffle on|off, /m_loop off|track|folder, /m_reload, /m_status."
        )

    def process_command(self, command_text: str) -> str:
        raw = _clean_command_text(command_text)
        if raw.startswith("/"):
            raw = raw[1:]
        if not raw:
            return self.command_help()
        parts = raw.split(" ", 1)
        cmd = parts[0].casefold()
        args = parts[1].strip() if len(parts) > 1 else ""

        legacy_map = {
            "k_mhelp": "m_help",
            "k_mplay": "m_play",
            "k_mfolder": "m_folder",
            "k_mplaylist": "m_playlist",
            "k_mfind": "m_find",
            "k_mpause": "m_pause",
            "k_mresume": "m_resume",
            "k_mstop": "m_stop",
            "k_mnext": "m_next",
            "k_mprev": "m_prev",
            "k_mvol": "m_vol",
            "k_mshuffle": "m_shuffle",
            "k_mloop": "m_loop",
            "k_mreload": "m_reload",
            "k_mstatus": "m_status",
        }
        cmd = legacy_map.get(cmd, cmd)

        if cmd == "m_help":
            return self.command_help()
        if cmd == "m_play":
            return self.play_song(args)
        if cmd == "m_folder":
            return self.play_folder(args)
        if cmd == "m_playlist":
            return self.play_playlist(args)
        if cmd == "m_find":
            return self.play_search(args)
        if cmd == "m_pause":
            return self.pause()
        if cmd == "m_resume":
            return self.resume()
        if cmd == "m_stop":
            return self.stop()
        if cmd == "m_next":
            return self.next_track()
        if cmd == "m_prev":
            return self.previous_track()
        if cmd == "m_vol":
            return self.set_volume(args)
        if cmd == "m_shuffle":
            return self.set_shuffle(args)
        if cmd == "m_loop":
            return self.set_loop(args)
        if cmd == "m_reload":
            return self.reload_index()
        if cmd == "m_status":
            return self.status_text()
        return f"[MUSIC] Unknown command: /{cmd}. Use /m_help."

    def _format_song_choice(self, songs: List[SongEntry]) -> Tuple[Optional[SongEntry], str]:
        if not songs:
            return None, ""
        ordered = sorted(songs, key=lambda item: item.sort_key)
        song = ordered[0]
        if len(ordered) == 1:
            return song, ""
        return song, f" Matched {len(ordered)} versions, using {song.relative_path}."

    def _resolve_song(self, query: str) -> Tuple[Optional[SongEntry], str]:
        if not _clean_command_text(query):
            return None, "[MUSIC] Usage: /m_play <song_name>"
        key = _normalize_text(query)
        exact = self._song_index.get(key, [])
        if exact:
            song, extra = self._format_song_choice(exact)
            return song, extra

        partial = [
            song
            for song in self._songs
            if key in _normalize_text(song.name) or key in _normalize_text(song.relative_path)
        ]
        if partial:
            song, extra = self._format_song_choice(partial)
            return song, extra
        return None, f"[MUSIC] Song not found: {query}"

    def _resolve_folder(self, query: str) -> Tuple[List[SongEntry], str]:
        if not _clean_command_text(query):
            return [], "[MUSIC] Usage: /m_folder <folder_name>"
        key = _normalize_text(query)
        songs = list(self._folder_index.get(key, []))
        if not songs:
            songs = [
                song
                for song in self._songs
                if key in _normalize_text(song.folder)
            ]
        songs = sorted(set(songs), key=lambda item: item.sort_key)
        if not songs:
            return [], f"[MUSIC] Folder not found: {query}"
        return songs, ""

    def _resolve_playlist(self, query: str) -> Tuple[List[SongEntry], List[str], str]:
        if not _clean_command_text(query):
            return [], [], "[MUSIC] Usage: /m_playlist <list_name>"
        key = _normalize_text(query)
        raw_entries = self._playlist_index.get(key)
        if raw_entries is None:
            return [], [], f"[MUSIC] Playlist not found: {query}"

        songs: List[SongEntry] = []
        missing: List[str] = []
        for item in raw_entries:
            song, _extra = self._resolve_song(item)
            if song is None:
                missing.append(item)
                continue
            songs.append(song)
        if not songs:
            return [], missing, f"[MUSIC] Playlist '{query}' has no valid songs."
        return songs, missing, ""

    def _build_queue(self, songs: List[SongEntry]) -> List[SongEntry]:
        queue = list(songs)
        if self.shuffle and len(queue) > 1:
            random.shuffle(queue)
        return queue

    def _play_index(self, index: int) -> str:
        ok, message = self._ensure_audio()
        if not ok:
            self.state = "stopped"
            return message
        if not self.queue:
            self.state = "stopped"
            self.current_index = -1
            return "[MUSIC] Queue is empty."
        if index < 0 or index >= len(self.queue):
            return "[MUSIC] Track index is out of range."
        track = self.queue[index]
        try:
            self._pygame.mixer.music.load(str(track.path))
            self._pygame.mixer.music.set_volume(self.volume_percent / 100.0)
            self._pygame.mixer.music.play()
        except Exception as exc:
            self.state = "stopped"
            return f"[MUSIC] Failed to play {track.relative_path}: {exc}"
        self.current_index = index
        self.state = "playing"
        self._last_play_started_at = time.monotonic()
        return f"[MUSIC] Playing {track.name} ({track.relative_path})."

    def play_song(self, query: str) -> str:
        with self._lock:
            song, extra = self._resolve_song(query)
            if song is None:
                return extra
            self.queue = [song]
            self.queue_label = song.name
            self.queue_mode = "single"
            result = self._play_index(0)
            return result + extra

    def play_folder(self, folder_name: str) -> str:
        with self._lock:
            songs, error = self._resolve_folder(folder_name)
            if not songs:
                return error
            self.queue = self._build_queue(songs)
            self.queue_label = folder_name
            self.queue_mode = "folder"
            result = self._play_index(0)
            return f"{result} [Folder: {folder_name}, {len(self.queue)} songs]"

    def play_playlist(self, playlist_name: str) -> str:
        with self._lock:
            songs, missing, error = self._resolve_playlist(playlist_name)
            if not songs:
                return error
            self.queue = self._build_queue(songs)
            self.queue_label = playlist_name
            self.queue_mode = "playlist"
            result = self._play_index(0)
            if not missing:
                return f"{result} [Playlist: {playlist_name}, {len(self.queue)} songs]"
            return (
                f"{result} [Playlist: {playlist_name}, {len(self.queue)} songs, "
                f"{len(missing)} missing entries]"
            )

    def play_search(self, keyword: str) -> str:
        with self._lock:
            key = _normalize_text(keyword)
            if not key:
                return "[MUSIC] Usage: /m_find <keyword>"
            matches = [
                song
                for song in self._songs
                if key in _normalize_text(song.name)
                or key in _normalize_text(song.relative_path)
                or key in _normalize_text(song.folder)
            ]
            matches = sorted(set(matches), key=lambda item: item.sort_key)
            if not matches:
                return f"[MUSIC] No songs matched: {keyword}"
            self.queue = self._build_queue(matches)
            self.queue_label = keyword
            self.queue_mode = "search"
            result = self._play_index(0)
            return f"{result} [Search: {keyword}, {len(self.queue)} songs]"

    def pause(self) -> str:
        with self._lock:
            if self._pygame is None or self.state != "playing":
                return "[MUSIC] Nothing is playing."
            try:
                self._pygame.mixer.music.pause()
            except Exception as exc:
                return f"[MUSIC] Pause failed: {exc}"
            self.state = "paused"
            return "[MUSIC] Paused."

    def resume(self) -> str:
        with self._lock:
            ok, message = self._ensure_audio()
            if not ok:
                return message
            if self.state != "paused":
                return "[MUSIC] Nothing is paused."
            try:
                self._pygame.mixer.music.unpause()
            except Exception as exc:
                return f"[MUSIC] Resume failed: {exc}"
            self.state = "playing"
            self._last_play_started_at = time.monotonic()
            return "[MUSIC] Resumed."

    def stop(self) -> str:
        with self._lock:
            if self._pygame is None or self.state == "stopped":
                self.state = "stopped"
                return "[MUSIC] Already stopped."
            try:
                self._pygame.mixer.music.stop()
            except Exception as exc:
                return f"[MUSIC] Stop failed: {exc}"
            self.state = "stopped"
            return "[MUSIC] Stopped."

    def next_track(self) -> str:
        with self._lock:
            if not self.queue:
                return "[MUSIC] Queue is empty."
            next_index = self.current_index + 1
            if next_index >= len(self.queue):
                if self.loop_mode == "folder":
                    next_index = 0
                else:
                    return "[MUSIC] Already at the end of the queue."
            return self._play_index(next_index)

    def previous_track(self) -> str:
        with self._lock:
            if not self.queue:
                return "[MUSIC] Queue is empty."
            prev_index = self.current_index - 1
            if prev_index < 0:
                if self.loop_mode == "folder":
                    prev_index = len(self.queue) - 1
                else:
                    prev_index = 0
            return self._play_index(prev_index)

    def set_volume(self, value: int | str) -> str:
        with self._lock:
            try:
                volume = int(float(str(value).strip()))
            except ValueError:
                return "[MUSIC] Usage: /m_vol <0-100>"
            volume = max(0, min(100, volume))
            self.volume_percent = volume
            if self._pygame is not None:
                try:
                    self._pygame.mixer.music.set_volume(self.volume_percent / 100.0)
                except Exception as exc:
                    return f"[MUSIC] Volume change failed: {exc}"
            return f"[MUSIC] Volume set to {self.volume_percent}%."

    def set_shuffle(self, value: str) -> str:
        with self._lock:
            text = _normalize_text(value)
            if text not in {"on", "off"}:
                return "[MUSIC] Usage: /m_shuffle on|off"
            self.shuffle = text == "on"
            state = "on" if self.shuffle else "off"
            return f"[MUSIC] Shuffle is {state} for future folders, playlists, and searches."

    def set_loop(self, value: str) -> str:
        with self._lock:
            mode = _normalize_text(value)
            if mode not in {"off", "track", "folder"}:
                return "[MUSIC] Usage: /m_loop off|track|folder"
            self.loop_mode = mode
            return f"[MUSIC] Loop mode set to {self.loop_mode}."

    def status_text(self) -> str:
        with self._lock:
            if not self.queue or self.current_index < 0:
                return (
                    f"[MUSIC] Status: {self.state}. No active queue. "
                    f"Volume {self.volume_percent}%, shuffle {'on' if self.shuffle else 'off'}, loop {self.loop_mode}."
                )
            current = self.queue[self.current_index]
            return (
                f"[MUSIC] Status: {self.state}. "
                f"Track {self.current_index + 1}/{len(self.queue)}: {current.name} ({current.relative_path}). "
                f"Volume {self.volume_percent}%, shuffle {'on' if self.shuffle else 'off'}, loop {self.loop_mode}."
            )

    def close(self) -> None:
        self._monitor_stop.set()
        if self._pygame is not None:
            try:
                self._pygame.mixer.music.stop()
            except Exception:
                pass
            try:
                self._pygame.mixer.quit()
            except Exception:
                pass
            try:
                self._pygame.quit()
            except Exception:
                pass
            self._pygame = None


class SentientSongsService:
    """Independent localhost backend for SentientSongs playback."""

    def __init__(self, config_path: Path | str | None = None):
        self.config_path = ensure_sentient_songs_layout(config_path)
        self.config = load_sentient_songs_config(self.config_path)
        self.manager = SentientSongsManager(config_path=self.config_path)
        self.app = Flask("SentientSongs")
        self._server = None
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._controllers: Dict[str, float] = {}
        self._started_at = time.monotonic()
        self._setup_routes()

    def _touch_controller(self, controller_name: str) -> None:
        name = _normalize_text(controller_name)
        if not name:
            return
        with self._lock:
            self._controllers[name] = time.monotonic()

    def _active_controllers(self) -> List[str]:
        now = time.monotonic()
        timeout = self.config.heartbeat_timeout
        active: List[str] = []
        stale: List[str] = []
        with self._lock:
            for name, last_seen in self._controllers.items():
                if now - last_seen <= timeout:
                    active.append(name)
                else:
                    stale.append(name)
            for name in stale:
                self._controllers.pop(name, None)
        return sorted(active)

    def _status_payload(self) -> Dict[str, object]:
        return {
            "ok": True,
            "service": "SentientSongs",
            "host": self.config.host,
            "port": self.config.port,
            "heartbeat_timeout": self.config.heartbeat_timeout,
            "active_controllers": self._active_controllers(),
            "status": self.manager.status_text(),
        }

    def _setup_routes(self) -> None:
        @self.app.get("/status")
        def status_route():
            return jsonify(self._status_payload())

        @self.app.post("/heartbeat")
        def heartbeat_route():
            data = request.get_json(silent=True) or {}
            controller = _clean_command_text(data.get("controller"))
            if not controller:
                return jsonify({"ok": False, "message": "Missing controller"}), 400
            self._touch_controller(controller)
            return jsonify({"ok": True, "active_controllers": self._active_controllers()})

        @self.app.post("/command")
        def command_route():
            data = request.get_json(silent=True) or {}
            controller = _clean_command_text(data.get("controller"))
            command = _clean_command_text(data.get("command"))
            if controller:
                self._touch_controller(controller)
            if not command:
                return jsonify({"ok": False, "message": "Missing command"}), 400
            return jsonify({"ok": True, "text": self.manager.process_command(command)})

        @self.app.post("/shutdown")
        def shutdown_route():
            data = request.get_json(silent=True) or {}
            controller = _normalize_text(data.get("controller"))
            if controller != "standalone":
                return jsonify({"ok": False, "message": "Shutdown is only available to standalone"}), 403
            self.request_shutdown()
            return jsonify({"ok": True, "message": "SentientSongs shutting down"})

    def request_shutdown(self) -> None:
        with self._lock:
            if self._shutdown_event.is_set():
                return
            self._shutdown_event.set()
        self.manager.close()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _heartbeat_monitor(self) -> None:
        while not self._shutdown_event.wait(1.0):
            if self._active_controllers():
                continue
            if time.monotonic() - self._started_at >= self.config.heartbeat_timeout:
                self.request_shutdown()
                break

    def serve_forever(self) -> None:
        threading.Thread(target=self._heartbeat_monitor, daemon=True).start()
        self._server = make_server(self.config.host, self.config.port, self.app, threaded=True)
        try:
            self._server.serve_forever()
        finally:
            self.manager.close()


_MANAGER: Optional[SentientSongsManager] = None
_MANAGER_LOCK = threading.Lock()


def get_sentient_songs_manager() -> SentientSongsManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = SentientSongsManager()
        return _MANAGER


def run_backend_service(config_path: Path | str | None = None) -> int:
    service = SentientSongsService(config_path=config_path)
    service.serve_forever()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SentientSongs backend service")
    parser.add_argument("--serve", action="store_true", help="Run the SentientSongs backend service")
    args = parser.parse_args(argv)
    if args.serve:
        return run_backend_service()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
