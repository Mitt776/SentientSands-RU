"""
apply_config.py — Push master configuration to all source files.

Reads config_master.txt and writes values to:
  [SentientSands]  -> SentientSands_Config.ini
  [Kayak]          -> Kayak/config/core_config.txt
  [Renaming]       -> server/config/renaming_rules.txt

Only keys present in config_master.txt are updated. Existing keys in the
target files that aren't mentioned here are left untouched. Comments in
target files are preserved.

Usage:
    python apply_config.py              (from the SentientSands folder)
    python apply_config.py --dry-run    (show what would change, don't write)
"""

import argparse
import configparser
import os
import re
import sys


# ─── PATHS ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(SCRIPT_DIR, "config_master.txt")

# Resolve SentientSands_Config.ini (same logic as configuration.py)
INI_PATH = os.path.join(SCRIPT_DIR, "SentientSands_Config.ini")
if not os.path.exists(INI_PATH):
    # Try parent directory (if script is inside a subdirectory)
    alt = os.path.join(os.path.dirname(SCRIPT_DIR), "SentientSands_Config.ini")
    if os.path.exists(alt):
        INI_PATH = alt

KAYAK_CONFIG_PATH = os.path.join(SCRIPT_DIR, "Kayak", "config", "core_config.txt")
RENAMING_RULES_PATH = os.path.join(SCRIPT_DIR, "server", "config", "renaming_rules.txt")


# ─── PARSE MASTER ────────────────────────────────────────────────────────────

def parse_master(path):
    """Parse config_master.txt into {section: {key: value}} dict."""
    sections = {}
    current_section = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()

            # Skip blanks and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Section header
            m = re.match(r"^\[(\w+)\]$", stripped)
            if m:
                current_section = m.group(1)
                sections[current_section] = {}
                continue

            # Key = value
            if current_section and "=" in stripped:
                key, val = stripped.split("=", 1)
                sections[current_section][key.strip()] = val.strip()

    return sections


# ─── WRITERS ─────────────────────────────────────────────────────────────────

def apply_ini(values, dry_run=False):
    """Write [SentientSands] values to SentientSands_Config.ini."""
    if not values:
        return 0

    config = configparser.ConfigParser()
    config.optionxform = str  # preserve PascalCase
    if os.path.exists(INI_PATH):
        config.read(INI_PATH)

    if "Settings" not in config:
        config["Settings"] = {}

    changed = 0
    for key, val in values.items():
        old = config["Settings"].get(key)
        if old != val:
            config["Settings"][key] = val
            changed += 1
            action = "DRY" if dry_run else "SET"
            print(f"  [{action}] INI: {key} = {val}" + (f"  (was: {old})" if old else ""))

    if not dry_run and changed:
        with open(INI_PATH, "w", encoding="utf-8") as f:
            config.write(f)
        print(f"  Wrote {INI_PATH}")

    return changed


def apply_keyvalue_file(path, values, label, dry_run=False):
    """Update a key = value file, preserving comments and structure.

    For each key in `values`: if the key exists in the file, update its
    value in place. If not, append it at the end.
    """
    if not values:
        return 0

    if not os.path.exists(path):
        print(f"  WARNING: {path} not found — creating it")
        if not dry_run:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for k, v in values.items():
                    f.write(f"{k} = {v}\n")
            print(f"  Wrote {path}")
        return len(values)

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    remaining = dict(values)
    changed = 0
    new_lines = []

    for line in lines:
        stripped = line.strip()

        # Preserve comments and blanks as-is
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        # Try to match key = value
        m = re.match(r"^([^=]+?)\s*=\s*(.*)$", stripped)
        if m:
            file_key = m.group(1).strip()
            file_val = m.group(2).strip()
            if file_key in remaining:
                new_val = remaining.pop(file_key)
                if file_val != new_val:
                    new_lines.append(f"{file_key} = {new_val}\n")
                    changed += 1
                    action = "DRY" if dry_run else "SET"
                    print(f"  [{action}] {label}: {file_key} = {new_val}  (was: {file_val})")
                else:
                    new_lines.append(line)  # unchanged
            else:
                new_lines.append(line)  # not in master, keep as-is
        else:
            new_lines.append(line)

    # Append any keys from master that weren't in the file
    for k, v in remaining.items():
        new_lines.append(f"{k} = {v}\n")
        changed += 1
        action = "DRY" if dry_run else "ADD"
        print(f"  [{action}] {label}: {k} = {v}  (new key)")

    if not dry_run and changed:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"  Wrote {path}")

    return changed


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Push config_master.txt to all SentientSands + Kayak config files."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing anything."
    )
    args = parser.parse_args()

    if not os.path.exists(MASTER_PATH):
        print(f"ERROR: {MASTER_PATH} not found.")
        print("Run this script from the SentientSands folder.")
        sys.exit(1)

    sections = parse_master(MASTER_PATH)
    total = 0

    print("=" * 60)
    print("  Applying config_master.txt")
    if args.dry_run:
        print("  (DRY RUN — no files will be written)")
    print("=" * 60)
    print()

    # 1. SentientSands INI
    ss_values = sections.get("SentientSands", {})
    if ss_values:
        print(f"[SentientSands] -> {INI_PATH}")
        total += apply_ini(ss_values, args.dry_run)
        print()

    # 2. Kayak core config
    kayak_values = sections.get("Kayak", {})
    if kayak_values:
        print(f"[Kayak] -> {KAYAK_CONFIG_PATH}")
        total += apply_keyvalue_file(KAYAK_CONFIG_PATH, kayak_values, "Kayak", args.dry_run)
        print()

    # 3. Renaming rules
    rename_values = sections.get("Renaming", {})
    if rename_values:
        print(f"[Renaming] -> {RENAMING_RULES_PATH}")
        total += apply_keyvalue_file(RENAMING_RULES_PATH, rename_values, "Renaming", args.dry_run)
        print()

    # Summary
    print("=" * 60)
    if total == 0:
        print("  No changes needed — all values already match.")
    elif args.dry_run:
        print(f"  {total} value(s) would be updated. Run without --dry-run to apply.")
    else:
        print(f"  {total} value(s) updated. Restart the server for changes to take effect.")
    print("=" * 60)


if __name__ == "__main__":
    main()
