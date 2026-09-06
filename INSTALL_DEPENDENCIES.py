#!/usr/bin/env python3
"""
SentientSands unified dependency installer.
Place this file in the SentientSands root folder and run INSTALL_DEPENDENCIES.bat on Windows,
or run this file directly with Python on Linux/macOS.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent

CORE_PACKAGES = [
    "flask",
    "flask-cors",
    "requests",
]

IMPORT_TESTS = [
    ("flask", "flask"),
    ("flask_cors", "flask-cors"),
    ("requests", "requests"),
]

REQUIREMENT_FILES = [
    ROOT / "requirements.txt",
    ROOT / "server" / "requirements.txt",
    ROOT / "Kayak" / "requirements.txt",
]

CONFIG_DIR_CANDIDATES = [
    ROOT / "server" / "config",
    ROOT / "server" / "configs",
]


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def run_command(cmd: Sequence[str], label: str) -> bool:
    print(f"\n[RUN] {label}")
    print("      " + " ".join(f'\"{c}\"' if " " in c else c for c in cmd))
    try:
        result = subprocess.run(
            list(cmd),
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] Command not found: {exc}")
        return False
    except Exception as exc:
        print(f"[ERROR] Could not run command: {exc}")
        return False

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(f"[ERROR] {label} failed with exit code {result.returncode}.")
        if result.stderr.strip():
            print("\n--- ERROR OUTPUT ---")
            print(result.stderr.strip())
            print("--- END ERROR OUTPUT ---")
        return False

    if result.stderr.strip():
        # pip sometimes writes harmless warnings to stderr. Show them, but do not fail.
        print("\n--- WARNINGS ---")
        print(result.stderr.strip())
        print("--- END WARNINGS ---")
    print(f"[OK] {label}")
    return True


def ensure_pip() -> bool:
    # First check whether pip already works.
    if run_command([sys.executable, "-m", "pip", "--version"], "Checking pip"):
        return True

    print("\n[INFO] pip was not available. Trying ensurepip...")
    if not run_command([sys.executable, "-m", "ensurepip", "--upgrade"], "Installing pip with ensurepip"):
        return False
    return run_command([sys.executable, "-m", "pip", "--version"], "Checking pip again")


def install_requirements() -> bool:
    reqs = [p for p in REQUIREMENT_FILES if p.exists()]
    if reqs:
        for req in reqs:
            rel = req.relative_to(ROOT)
            if not run_command([sys.executable, "-m", "pip", "install", "-r", str(req)], f"Installing {rel}"):
                return False
        return True

    print("\n[INFO] No requirements.txt files found. Installing core SentientSands/Kayak packages.")
    return run_command([sys.executable, "-m", "pip", "install", *CORE_PACKAGES], "Installing core packages")


def test_imports() -> bool:
    banner("Testing installed dependencies")
    ok = True
    for import_name, package_name in IMPORT_TESTS:
        try:
            __import__(import_name)
            print(f"[OK] import {import_name} ({package_name})")
        except Exception as exc:
            print(f"[ERROR] Could not import {import_name} ({package_name}): {exc}")
            ok = False
    return ok


def get_config_dir() -> Path:
    for path in CONFIG_DIR_CANDIDATES:
        if path.exists():
            return path
    # Prefer the actual current SentientSands path, but support old typo/path if present.
    path = ROOT / "server" / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_json_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    print(f"[INFO] Created missing config file: {path}")


def open_files(paths: Iterable[Path]) -> None:
    system = platform.system().lower()
    for path in paths:
        try:
            if system == "windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif system == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                opener = shutil.which("xdg-open")
                if opener:
                    subprocess.Popen([opener, str(path)])
                else:
                    print(f"[INFO] Could not auto-open. Edit manually: {path}")
        except Exception as exc:
            print(f"[WARN] Could not open {path}: {exc}")


def main() -> int:
    banner("SentientSands Unified Dependency Installer")
    print(f"Root folder: {ROOT}")
    print(f"Python: {sys.executable}")

    if not ensure_pip():
        banner("Installation failed")
        print("pip could not be installed or detected.")
        print("Install Python 3.10+ with pip enabled, then run this installer again.")
        input("\nPress Enter to close...")
        return 1

    banner("Installing dependencies")
    if not install_requirements():
        banner("Installation failed")
        print("One or more dependency installation commands failed.")
        print("Read the error output above, then run this installer again after fixing it.")
        input("\nPress Enter to close...")
        return 1

    if not test_imports():
        banner("Dependency test failed")
        print("Dependencies were installed, but at least one import test failed.")
        print("Read the errors above. This usually means the wrong Python environment is being used.")
        input("\nPress Enter to close...")
        return 1

    config_dir = get_config_dir()
    providers = config_dir / "providers.json"
    models = config_dir / "models.json"
    ensure_json_file(providers)
    ensure_json_file(models)

    banner("Success")
    print("All dependencies installed.")
    print("Insert your provider and model on the models.json and providers.json.")
    print(f"Config folder: {config_dir}")
    input("\nPress Enter to close this installer and open the .json files...")
    open_files([providers, models])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
