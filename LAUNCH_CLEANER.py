#!/usr/bin/env python3
"""
SentientSands Launch Cleaner
Interactive cleanup tool for preparing a release/dev copy of the mod.
Run from the SentientSands root, or use LAUNCH_CLEANER.bat.
"""
from __future__ import annotations

import json
import os
import shutil
import string
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
KEEP_CAMPAIGNS = {"default", "template"}
CAMPAIGN_RUNTIME_STATE_FILES = {"event_history.json", "event_story.json"}

DEFAULT_PROVIDERS = {
    "openrouter": {
        "api_key": "YOUR_OPENROUTER_KEY",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "player2": {
        "api_key": "sk-player2-local",
        "base_url": "http://127.0.0.1:4315/v1",
    },
    "nanogpt": {
        "api_key": "YOUR_NANOGPT_KEY",
        "base_url": "https://nano-gpt.com/api/subscription/v1",
    },
    "ollama": {
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
    },
    "lmstudio_local": {
        "api_key": "YOUR_LMSTUDIO_LOCAL_KEY",
        "base_url": "http://127.0.0.1:1234/v1",
    },
    "openai": {
        "api_key": "YOUR_OPENAI_KEY",
        "base_url": "https://api.openai.com/v1",
    },
}

DEFAULT_MODELS = {
    "kimi-k2.5": {"provider": "openrouter", "model": "moonshotai/kimi-k2.5"},
    "deepseek-v3.2": {"provider": "openrouter", "model": "deepseek/deepseek-v3.2"},
    "glm-4.7": {"provider": "openrouter", "model": "z-ai/glm-4.7"},
    "grok-4.1-fast": {"provider": "openrouter", "model": "x-ai/grok-4.1-fast"},
    "minimax-m2.5": {"provider": "openrouter", "model": "minimax/minimax-m2.5"},
    "gemini-3-flash": {"provider": "openrouter", "model": "google/gemini-3-flash-preview"},
    "gemini-2.5-flash": {"provider": "openrouter", "model": "google/gemini-2.5-flash"},
    "glm-5": {"provider": "nanogpt", "model": "zai-org/glm-5"},
    "nano-glm-4.7": {"provider": "nanogpt", "model": "zai-org/glm-4.7"},
    "nano-glm-4.6-derestricted": {"provider": "nanogpt", "model": "GLM-4.6-Derestricted-v5"},
    "mimo-v2-flash": {"provider": "openrouter", "model": "xiaomi/mimo-v2-flash"},
    "player2-default": {"provider": "player2", "model": "default"},
    "ollama-llama3": {"provider": "ollama", "model": "llama3"},
    "qwen3.5-9b": {"provider": "lmstudio_local", "model": "qwen3.5-9b"},
    "meta-llama-3.1-8b-instruct": {
        "provider": "lmstudio_local",
        "model": "meta-llama-3.1-8b-instruct@q6_k_l",
    },
    "gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"},
}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def ask_yes_no(question: str, default: bool | None = None) -> bool:
    suffix = " [Y/N]"
    if default is True:
        suffix = " [Y/n]"
    elif default is False:
        suffix = " [y/N]"
    while True:
        ans = input(question + suffix + ": ").strip().lower()
        if not ans and default is not None:
            return default
        if ans in {"y", "yes", "s", "sim"}:
            return True
        if ans in {"n", "no", "nao", "não"}:
            return False
        print("Please answer Y or N.")


def safe_delete_file(path: Path) -> tuple[bool, str | None]:
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
        return True, None
    except Exception as exc:
        return False, str(exc)


def safe_delete_dir(path: Path) -> tuple[bool, str | None]:
    try:
        if path.exists():
            shutil.rmtree(path)
        return True, None
    except Exception as exc:
        return False, str(exc)


def find_py_cache() -> tuple[list[Path], list[Path]]:
    pycache_dirs = [p for p in ROOT.rglob("__pycache__") if p.is_dir()]
    compiled = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix.lower() in {".pyc", ".pyo"}]
    # Files inside __pycache__ will be removed by directory removal. Keep them in the report,
    # but do not delete them separately after deleting the directory.
    return pycache_dirs, compiled


def clear_py_cache() -> tuple[int, list[str]]:
    errors: list[str] = []
    dirs, files = find_py_cache()
    deleted = 0

    for d in sorted(dirs, key=lambda x: len(str(x)), reverse=True):
        ok, err = safe_delete_dir(d)
        if ok:
            deleted += 1
        else:
            errors.append(f"{rel(d)} -> {err}")

    # Remove loose .pyc/.pyo files outside __pycache__.
    for f in files:
        if any(parent.name == "__pycache__" for parent in f.parents):
            continue
        ok, err = safe_delete_file(f)
        if ok:
            deleted += 1
        else:
            errors.append(f"{rel(f)} -> {err}")
    return deleted, errors


def find_logs() -> list[Path]:
    logs: set[Path] = set()
    for pattern in ("*.log", "*.log.*"):
        logs.update(p for p in ROOT.rglob(pattern) if p.is_file())
    return sorted(logs)


def clear_logs() -> tuple[int, list[str]]:
    errors: list[str] = []
    deleted = 0
    for f in find_logs():
        ok, err = safe_delete_file(f)
        if ok:
            deleted += 1
        else:
            errors.append(f"{rel(f)} -> {err}")
    return deleted, errors


def runtime_artifact_dirs() -> list[Path]:
    dirs = [
        ROOT / "sentient_sands_registry",
    ]
    campaigns = ROOT / "server" / "campaigns"
    if campaigns.exists():
        for campaign in campaigns.iterdir():
            if not campaign.is_dir():
                continue
            dirs.extend(
                [
                    campaign / "sentient_sands_registry",
                    campaign / "hydration_jobs",
                ]
            )
    return dirs


def runtime_artifact_files() -> list[Path]:
    files: list[Path] = []
    server = ROOT / "server"
    campaigns = server / "campaigns"

    for filename in CAMPAIGN_RUNTIME_STATE_FILES:
        legacy_path = server / filename
        if legacy_path.is_file():
            files.append(legacy_path)

    if campaigns.exists():
        for campaign in campaigns.iterdir():
            if not campaign.is_dir():
                continue
            for filename in CAMPAIGN_RUNTIME_STATE_FILES:
                path = campaign / filename
                if path.is_file():
                    files.append(path)
    return files


def find_runtime_artifacts() -> list[Path]:
    files: list[Path] = []
    for folder in runtime_artifact_dirs():
        if folder.exists():
            files.extend(p for p in folder.rglob("*") if p.is_file())
    files.extend(runtime_artifact_files())
    return sorted(set(files))



def clear_runtime_artifacts() -> tuple[int, list[str]]:
    errors: list[str] = []
    deleted = 0
    touched_dirs = [d for d in runtime_artifact_dirs() if d.exists()]

    for f in find_runtime_artifacts():
        ok, err = safe_delete_file(f)
        if ok:
            deleted += 1
        else:
            errors.append(f"{rel(f)} -> {err}")

    # Keep the known runtime folders themselves, but remove any empty subfolders under them.
    for base in touched_dirs:
        for d in sorted([p for p in base.rglob("*") if p.is_dir()], key=lambda x: len(str(x)), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    return deleted, errors


def active_campaign_config_files() -> list[Path]:
    return [
        ROOT / "SentientSands_Config.ini",
        ROOT / "config_master.txt",
    ]


def reset_active_campaign() -> tuple[int, list[str]]:
    errors: list[str] = []
    written = 0
    for path in active_campaign_config_files():
        if not path.exists():
            errors.append(f"{rel(path)} -> file not found")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines: list[str] = []
            found = False
            changed = False
            for line in lines:
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                body = line[:-len(newline)] if newline else line
                if "=" in body and body.split("=", 1)[0].strip().lower() == "activecampaign":
                    indent = body[:len(body) - len(body.lstrip())]
                    replacement = f"{indent}ActiveCampaign = Default{newline}"
                    new_lines.append(replacement)
                    found = True
                    changed = changed or replacement != line
                else:
                    new_lines.append(line)

            if not found:
                errors.append(f"{rel(path)} -> ActiveCampaign setting not found")
                continue
            if changed:
                path.write_text("".join(new_lines), encoding="utf-8")
            written += 1
        except Exception as exc:
            errors.append(f"{rel(path)} -> {exc}")
    return written, errors


def clear_favorites_config() -> tuple[int, list[str]]:
    errors: list[str] = []
    written = 0
    for path in active_campaign_config_files():
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines: list[str] = []
            found = False
            changed = False
            for line in lines:
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                body = line[:-len(newline)] if newline else line
                if "=" in body and body.split("=", 1)[0].strip().lower() == "favorites":
                    indent = body[:len(body) - len(body.lstrip())]
                    replacement = f"{indent}Favorites = {newline}"
                    new_lines.append(replacement)
                    found = True
                    changed = changed or replacement != line
                else:
                    new_lines.append(line)

            if not found:
                continue
            if changed:
                path.write_text("".join(new_lines), encoding="utf-8")
            written += 1
        except Exception as exc:
            errors.append(f"{rel(path)} -> {exc}")
    return written, errors


def campaign_roots() -> list[Path]:
    return [
        ROOT / "Kayak" / "KayakDB" / "Campaigns",
        ROOT / "server" / "campaigns",
    ]


def discover_campaigns() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for base in campaign_roots():
        if not base.exists():
            continue
        for p in base.iterdir():
            if not p.is_dir():
                continue
            if p.name.lower() in KEEP_CAMPAIGNS:
                continue
            found.setdefault(p.name, []).append(p)
    return dict(sorted(found.items(), key=lambda item: item[0].lower()))


def backup_campaigns(campaign_names: Iterable[str], campaign_map: dict[str, list[Path]]) -> Path | None:
    names = [n for n in campaign_names if n in campaign_map]
    if not names:
        return None
    backup_dir = ROOT / "_launch_cleaner_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = backup_dir / f"campaign_backup_{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            for folder in campaign_map.get(name, []):
                for path in folder.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(ROOT))
    return zip_path


def delete_campaigns(campaign_names: Iterable[str], campaign_map: dict[str, list[Path]]) -> tuple[int, list[str]]:
    errors: list[str] = []
    deleted = 0
    for name in campaign_names:
        for folder in campaign_map.get(name, []):
            ok, err = safe_delete_dir(folder)
            if ok:
                deleted += 1
            else:
                errors.append(f"{rel(folder)} -> {err}")
    return deleted, errors


def select_specific_campaigns(campaign_map: dict[str, list[Path]]) -> list[str]:
    names = list(campaign_map)
    if not names:
        print("No deletable campaigns found.")
        return []

    letters = string.ascii_uppercase
    key_to_name: dict[str, str] = {}
    print("\nExisting deletable campaigns:")
    for idx, name in enumerate(names):
        key = letters[idx] if idx < len(letters) else str(idx + 1)
        key_to_name[key] = name
        print(f"  {key}: {name}")

    raw = input("Which campaigns to delete? Type letters like AB, names separated by comma, or skip: ").strip()
    if not raw or raw.lower() == "skip":
        return []

    selected: list[str] = []
    if "," in raw:
        wanted = [x.strip() for x in raw.split(",") if x.strip()]
        lower_map = {n.lower(): n for n in names}
        for item in wanted:
            resolved = lower_map.get(item.lower())
            if resolved and resolved not in selected:
                selected.append(resolved)
    else:
        compact = raw.upper().replace(" ", "")
        for ch in compact:
            if ch in key_to_name and key_to_name[ch] not in selected:
                selected.append(key_to_name[ch])

    if not selected:
        print("No valid campaigns selected.")
    return selected


def reset_provider_model_configs() -> tuple[int, list[str]]:
    errors: list[str] = []
    written = 0
    targets = [
        (ROOT / "server" / "config" / "providers.json", DEFAULT_PROVIDERS),
        (ROOT / "server" / "config" / "models.json", DEFAULT_MODELS),
    ]
    for path, data in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
            written += 1
        except Exception as exc:
            errors.append(f"{rel(path)} -> {exc}")
    return written, errors


def songs_db_dir() -> Path:
    return ROOT / "Kayak" / "bridges" / "SentientSongs" / "SongsDB"


def find_song_files() -> list[Path]:
    base = songs_db_dir()
    if not base.exists():
        return []
    # Keep README.txt so the folder still documents itself.
    return [p for p in base.rglob("*") if p.is_file() and p.name.lower() != "readme.txt"]


def clear_songs_db() -> tuple[int, list[str]]:
    errors: list[str] = []
    deleted = 0
    for f in find_song_files():
        ok, err = safe_delete_file(f)
        if ok:
            deleted += 1
        else:
            errors.append(f"{rel(f)} -> {err}")
    # Remove empty subfolders under SongsDB, but keep SongsDB itself.
    base = songs_db_dir()
    if base.exists():
        for d in sorted([p for p in base.rglob("*") if p.is_dir()], key=lambda x: len(str(x)), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    return deleted, errors


def count_summary(actions: dict[str, object]) -> list[str]:
    lines: list[str] = []
    if actions.get("pycache"):
        dirs, files = find_py_cache()
        lines.append(f"Python cache: {len(dirs)} __pycache__ folders, {len(files)} .pyc/.pyo files")
    if actions.get("logs"):
        logs = find_logs()
        lines.append(f"Logs: {len(logs)} .log/.log.* files")
    if actions.get("runtime_artifacts"):
        files = find_runtime_artifacts()
        lines.append(f"Runtime registries/jobs/event history: {len(files)} generated file(s)")
    if actions.get("campaign_config"):
        lines.append("Launch config: reset ActiveCampaign to Default")
    if actions.get("favorites"):
        lines.append("Favorites: clear personal pinned NPC ids from launch config")
    if actions.get("campaigns"):
        names = actions.get("campaign_names") or []
        lines.append(f"Campaigns: {len(names)} campaign name(s): {', '.join(names) if names else 'none'}")
    if actions.get("configs"):
        lines.append("Provider/model configs: reset providers.json and models.json")
    if actions.get("songs"):
        songs = find_song_files()
        lines.append(f"SentientSongs: {len(songs)} generated file(s) in SongsDB (README kept)")
    return lines or ["No actions selected."]


def run_actions(actions: dict[str, object]) -> None:
    all_errors: list[str] = []
    print("\nRunning cleanup...\n")

    if actions.get("pycache"):
        n, errors = clear_py_cache()
        print(f"Python cache cleaned: {n} item(s)")
        all_errors.extend(errors)

    if actions.get("logs"):
        n, errors = clear_logs()
        print(f"Logs deleted: {n} file(s)")
        all_errors.extend(errors)

    if actions.get("runtime_artifacts"):
        n, errors = clear_runtime_artifacts()
        print(f"Runtime registries/jobs/event history deleted: {n} file(s)")
        all_errors.extend(errors)

    if actions.get("campaign_config"):
        n, errors = reset_active_campaign()
        print(f"ActiveCampaign config files reset: {n}")
        all_errors.extend(errors)

    if actions.get("favorites"):
        n, errors = clear_favorites_config()
        print(f"Favorites config entries cleared: {n}")
        all_errors.extend(errors)

    if actions.get("campaigns"):
        campaign_map = discover_campaigns()
        names = list(actions.get("campaign_names") or [])
        if actions.get("backup_campaigns"):
            zip_path = backup_campaigns(names, campaign_map)
            if zip_path:
                print(f"Campaign backup created: {rel(zip_path)}")
            else:
                print("Campaign backup skipped: no selected campaigns found.")
        n, errors = delete_campaigns(names, campaign_map)
        print(f"Campaign folders deleted: {n}")
        all_errors.extend(errors)

    if actions.get("configs"):
        n, errors = reset_provider_model_configs()
        print(f"Provider/model config files reset: {n}")
        all_errors.extend(errors)

    if actions.get("songs"):
        n, errors = clear_songs_db()
        print(f"SentientSongs generated files deleted: {n}")
        all_errors.extend(errors)

    if all_errors:
        print("\nSome items could not be cleaned:")
        for err in all_errors:
            print(f"  - {err}")
    else:
        print("\nCleanup finished with no reported errors.")


def build_actions() -> dict[str, object]:
    actions: dict[str, object] = {
        "pycache": False,
        "logs": False,
        "runtime_artifacts": False,
        "campaign_config": False,
        "favorites": False,
        "campaigns": False,
        "campaign_names": [],
        "backup_campaigns": False,
        "configs": False,
        "songs": False,
    }

    print("SentientSands Launch Cleaner")
    print(f"Root: {ROOT}\n")

    clear_all = ask_yes_no("0. CLEAR ALL safe launch cleanup", False)
    if clear_all:
        actions.update({
            "pycache": True,
            "logs": True,
            "runtime_artifacts": True,
            "campaign_config": True,
            "favorites": True,
            "configs": True,
            "songs": True,
        })
        campaign_map = discover_campaigns()
        names = list(campaign_map)
        if names:
            actions["campaigns"] = True
            actions["campaign_names"] = names
            actions["backup_campaigns"] = ask_yes_no("Backup campaigns first", True)
        return actions

    actions["pycache"] = ask_yes_no("1. Clear Python cache (__pycache__, .pyc, .pyo)", True)
    actions["logs"] = ask_yes_no("2. Clear all .log and .log.* files", True)
    actions["runtime_artifacts"] = ask_yes_no("3. Clear runtime registries, hydration jobs, and event history", True)
    actions["campaign_config"] = ask_yes_no("4. Reset ActiveCampaign to Default in launch configs", True)
    actions["favorites"] = ask_yes_no("5. Clear Favorites from launch config", True)

    campaign_map = discover_campaigns()
    if campaign_map:
        delete_all_campaigns = ask_yes_no("6. Delete all campaigns except Default and Template", False)
        selected: list[str] = []
        if delete_all_campaigns:
            selected = list(campaign_map)
        else:
            if ask_yes_no("6. Delete specific campaigns", False):
                selected = select_specific_campaigns(campaign_map)
        if selected:
            actions["campaigns"] = True
            actions["campaign_names"] = selected
            actions["backup_campaigns"] = ask_yes_no("6.1 Backup selected campaigns first", True)
    else:
        print("6. No deletable campaigns found. Skipping campaign cleanup.")

    actions["configs"] = ask_yes_no("7. Reset providers.json and models.json", False)
    actions["songs"] = ask_yes_no("8. Delete generated SentientSongs files from SongsDB", False)
    return actions


def main() -> int:
    try:
        actions = build_actions()
        print("\nCleanup summary:")
        for line in count_summary(actions):
            print(f"  - {line}")

        if not any(actions.get(k) for k in ("pycache", "logs", "runtime_artifacts", "campaign_config", "favorites", "campaigns", "configs", "songs")):
            print("\nNothing selected. Exiting.")
            return 0

        if ask_yes_no("Proceed with cleanup", False):
            run_actions(actions)
        else:
            print("Cleanup cancelled. Nothing was deleted.")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
