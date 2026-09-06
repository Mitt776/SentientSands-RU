# Changelog

## 0.5.0 — 2026-07-20

Release-preparation update:

- Updated the in-game welcome credit to “SentientSands Kayak by Harvicus and Pineaxe.”
- Moved Pineaxe from the contributor line to the primary developer credit; retained Wirlocke and ConcreteFoundry as contributors.
- Synchronized the matching C++ source and bundled UI localizations.
- Updated release metadata and documentation from 0.4.x to 0.5.0.
- Synchronized the embedded-Python setup script with the bundled Python 3.13.2 runtime.
- Replaced the ambiguous, misspelled Kayak terms file with GPLv3 section 7(b) attribution terms in `Kayak/ADDITIONAL_TERMS.md`.
- Removed an abandoned prototype name from public documentation, comments, and the one internal marker-regex identifier without changing its behavior.
- Added README, credits, project links, third-party notices, source notes, and checksums.
- Prepared the matching native source for separate publication through the official source repository.

No gameplay, save-data, prompt, retrieval, networking, or hook behavior was intentionally changed. The DLL was not recompiled: only two NUL-terminated UI strings were replaced in place, and the PE checksum was recalculated.
