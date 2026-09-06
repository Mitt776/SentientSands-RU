import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

URL = os.environ.get("SS_TEST_CHAT_URL", "http://127.0.0.1:5000/chat")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CHAR_DIR = SCRIPT_DIR.parent / "campaigns" / "Default" / "characters"
CHAR_DIR = Path(os.environ.get("SS_TEST_CHAR_DIR", str(DEFAULT_CHAR_DIR)))
REQUEST_TIMEOUT = 15


def post_json(payload, label):
    try:
        resp = requests.post(URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: {label} request failed: {exc}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"ERROR: {label} response is not JSON (status={resp.status_code})")
        print((resp.text or "")[:300])
        return None


def maybe_remove(path: Path):
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def main():
    print(f"Chat URL: {URL}")
    print(f"Character dir: {CHAR_DIR}")

    pre_payload = {
        "npc": "Masaru",
        "player": "Drifter",
        "message": "Who goes there?",
        "context": json.dumps({
            "name": "Masaru",
            "faction": "Tech Hunters",
            "race": "Shek",
            "gender": "Male",
            "job": "Wandering",
            "money": 500,
            "runtime_id": "99991",
        }),
    }

    print("\n== Sending PRE-RECRUITMENT context (Tech Hunters) ==")
    r1 = post_json(pre_payload, "PRE-RECRUITMENT")
    if r1 is None:
        return 1
    print(r1)

    print("Waiting 15 seconds for LLM batch thread to save the character...")
    time.sleep(15)

    s1 = hashlib.blake2s(b"Masaru_Tech_Hunters", digest_size=6).hexdigest()
    f1 = CHAR_DIR / f"Masaru__{s1}.cfg"
    if f1.exists():
        print(f"SUCCESS: PRE-RECRUIT file exists: {f1}")
    else:
        print(f"WARNING: PRE-RECRUIT file missing! {f1}")

    post_payload = {
        "npc": "Masaru",
        "player": "Drifter",
        "message": "I follow you now, boss.",
        "context": json.dumps({
            "name": "Masaru",
            "faction": "Nameless",
            "origin_faction": "Unknown",
            "race": "Shek",
            "gender": "Male",
            "job": "Following",
            "runtime_id": "14221",
        }),
    }

    print("\n== Sending POST-RECRUITMENT context (Nameless) with NEW memory handle ==")
    r2 = post_json(post_payload, "POST-RECRUITMENT")
    if r2 is None:
        return 1
    print(r2)

    print("Waiting 10 seconds for save...")
    time.sleep(10)

    s2 = hashlib.blake2s(b"Masaru_Nameless", digest_size=6).hexdigest()
    f2 = CHAR_DIR / f"Masaru__{s2}.cfg"

    if f2.exists():
        print(f"FAILURE: System fragmented the identity and created {f2}")
    else:
        print(f"SUCCESS: The system anchored to original config instead of fragmenting to {f2}")

    maybe_remove(f1)
    maybe_remove(f2)
    maybe_remove(CHAR_DIR / "history" / f"Masaru__{s1}_History.txt")
    maybe_remove(CHAR_DIR / "history" / f"Masaru__{s2}_History.txt")

    print("Test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
