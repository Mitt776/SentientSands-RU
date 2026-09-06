# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

from __future__ import annotations

import sys
from pathlib import Path


KAYAK_ROOT = Path(__file__).resolve().parents[2]
if str(KAYAK_ROOT) not in sys.path:
    sys.path.insert(0, str(KAYAK_ROOT))

from bridges.SentientSongs import SentientSongsClient


def main() -> int:
    client = SentientSongsClient(controller_name="standalone", auto_start=True)
    ok, message = client.start_heartbeat()
    if not ok:
        print(message)
        return 1

    print("SentientSongs standalone console")
    print("Type /m_help for commands. Type /m_exit to close this console.")
    print(client.status_text())

    try:
        while True:
            try:
                raw = input("SentientSongs> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue
            if raw.casefold() in {"/m_exit", "m_exit", "/k_mexit", "k_mexit"}:
                print(client.shutdown_service())
                print("SentientSongs closed.")
                return 0

            print(client.send_command(raw))
    finally:
        client.stop_heartbeat()

    print("Standalone closed. SentientSongs will shut down automatically after the heartbeat timeout if no controllers remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
