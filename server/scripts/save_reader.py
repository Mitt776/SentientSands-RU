import os
import re
import struct
import logging

_NAME_FIELD_MARKER = b'name'
_MAX_NAME_LENGTH = 80
_LEGACY_JUNK_WORDS = {'The', 'And', 'But', 'For', 'With', 'From', 'This'}


def _ordered_unique(values):
    seen = set()
    out = []
    for value in values:
        key = str(value).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _safe_registry_stem(name):
    return re.sub(r'[^\w\s-]', '', str(name or '')).strip().replace(' ', '_')


def _is_ascii_printable(raw):
    return bool(raw) and all(32 <= b <= 126 for b in raw)


def _looks_like_character_name(name):
    text = re.sub(r'\s+', ' ', str(name or '').strip())
    if not text or len(text) > _MAX_NAME_LENGTH:
        return False
    if text in _LEGACY_JUNK_WORDS:
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    if any(ch in text for ch in '{}[]<>'):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9' ._-]*", text))


def _scan_platoon_name_fields(data):
    names = []
    start = 0
    while True:
        idx = data.find(_NAME_FIELD_MARKER, start)
        if idx == -1:
            break

        length_pos = idx + len(_NAME_FIELD_MARKER)
        if length_pos + 4 <= len(data):
            name_len = struct.unpack_from('<I', data, length_pos)[0]
            name_pos = length_pos + 4
            if 1 <= name_len <= _MAX_NAME_LENGTH and name_pos + name_len <= len(data):
                raw_name = data[name_pos:name_pos + name_len]
                if _is_ascii_printable(raw_name):
                    decoded = re.sub(r'\s+', ' ', raw_name.decode('utf-8', errors='ignore').strip())
                    if _looks_like_character_name(decoded):
                        names.append(decoded)

        start = idx + 1

    return _ordered_unique(names)


def _scan_platoon_legacy_tokens(data):
    matches = re.findall(b'([A-Z][a-z]{2,15})', data)
    names = []
    for m in matches:
        name = m.decode('utf-8')
        if name in _LEGACY_JUNK_WORDS:
            continue
        names.append(name)
    return _ordered_unique(names)


def cleanup_fragment_registry_files(registry_dir, index):
    if not os.path.isdir(registry_dir) or not index:
        return 0

    known_names = {
        re.sub(r'\s+', ' ', str(name).strip())
        for name in index.keys()
        if str(name).strip()
    }
    known_names_lower = {name.lower() for name in known_names}
    removed = 0

    for full_name, platoons in index.items():
        clean_full_name = re.sub(r'\s+', ' ', str(full_name or '').strip())
        parts = [part for part in clean_full_name.split() if part]
        if len(parts) < 2 or not platoons:
            continue

        for part in parts:
            if part.lower() in known_names_lower:
                continue

            stem = _safe_registry_stem(part)
            if not stem:
                continue

            reg_file = os.path.join(registry_dir, f"{stem}_init.txt")
            if not os.path.exists(reg_file):
                continue

            try:
                with open(reg_file, 'r', encoding='utf-8') as rf:
                    content = rf.read()
            except OSError:
                continue

            if not any(platoon in content for platoon in platoons):
                continue

            try:
                os.remove(reg_file)
                removed += 1
                logging.info(
                    f"Removed split registry fragment '{part}' for multi-word name '{clean_full_name}'"
                )
            except OSError as e:
                logging.warning(f"Failed to remove split registry fragment {reg_file}: {e}")

    return removed


def _list_save_dirs():
    local_app_data = os.environ.get('LOCALAPPDATA')
    if not local_app_data:
        return []
    save_path = os.path.join(local_app_data, 'kenshi', 'save')
    if not os.path.exists(save_path):
        return []
    saves = [
        os.path.join(save_path, d)
        for d in os.listdir(save_path)
        if os.path.isdir(os.path.join(save_path, d))
    ]
    saves.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return saves

def get_latest_save():
    saves = _list_save_dirs()
    if not saves:
        return None

    # Prefer the most recent save that actually has platoon data.
    # Some slots (for example _current1) can exist with no platoon files yet.
    for candidate in saves:
        platoon_dir = os.path.join(candidate, 'platoon')
        if not os.path.isdir(platoon_dir):
            continue
        try:
            if any(name.endswith('.platoon') for name in os.listdir(platoon_dir)):
                return candidate
        except OSError:
            continue

    # Fallback: preserve old behavior if no populated save was found.
    return saves[0]

def scan_platoon_for_characters(platoon_path):
    """
    Scans a .platoon file for character names and serials.
    Kenshi .platoon format is complex, but we can extract names and nearby IDs.
    """
    try:
        with open(platoon_path, 'rb') as f:
            data = f.read()

        # Prefer the explicit `name` field stored inside Kenshi platoon blobs.
        # This preserves multi-word names such as "Bull Truckerson" as one
        # character instead of fragmenting them into capitalized tokens.
        names = _scan_platoon_name_fields(data)
        if names:
            return names

        # Fallback for unexpected/older layouts.
        return _scan_platoon_legacy_tokens(data)
    except Exception as e:
        logging.error(f"Error scanning {platoon_path}: {e}")
        return []

def build_world_index():
    saves = _list_save_dirs()
    if not saves:
        logging.warning("No Kenshi saves found.")
        return {}

    # Determine mod directory relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mod_dir = os.path.dirname(os.path.dirname(script_dir))

    # Since save_reader doesn't know active campaign, use root-level registry fallback.
    registry_dir = os.path.join(mod_dir, "sentient_sands_registry")
    if not os.path.exists(registry_dir):
        dev_reg = os.path.join(mod_dir, "SentientSands_Mod", "sentient_sands_registry")
        if os.path.exists(dev_reg):
            registry_dir = dev_reg
    if not os.path.exists(registry_dir):
        os.makedirs(registry_dir)

    best_index = {}
    for candidate in saves[:6]:
        logging.info(f"Scanning save: {candidate}")
        platoon_dir = os.path.join(candidate, 'platoon')
        if not os.path.isdir(platoon_dir):
            continue

        index = {}
        try:
            entries = os.listdir(platoon_dir)
        except OSError:
            continue

        for f in entries:
            if not f.endswith('.platoon'):
                continue
            chars = scan_platoon_for_characters(os.path.join(platoon_dir, f))
            for name in chars:
                if name not in index:
                    index[name] = []
                index[name].append(f)

                clean_name = re.sub(r'[^\w\s-]', '', name).strip()
                if not clean_name:
                    continue
                reg_file = os.path.join(registry_dir, f"{clean_name.replace(' ', '_')}_init.txt")
                if not os.path.exists(reg_file):
                    with open(reg_file, "w", encoding="utf-8") as rf:
                        rf.write(f"Registry: {name} initialized from save persistence ({f}).\n")

        removed = cleanup_fragment_registry_files(registry_dir, index)
        if removed:
            logging.info(f"Registry cleanup removed {removed} split fragments from {registry_dir}")

        if len(index) > len(best_index):
            best_index = index
        if index:
            logging.info(f"World index candidate accepted: {candidate} ({len(index)} names)")
            return index

    if best_index:
        removed = cleanup_fragment_registry_files(registry_dir, best_index)
        if removed:
            logging.info(f"Registry cleanup removed {removed} split fragments from {registry_dir}")
        logging.info(f"World index fallback accepted ({len(best_index)} names)")
    return best_index

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = build_world_index()
    for name, files in list(idx.items())[:10]:
        print(f"{name}: {files}")
