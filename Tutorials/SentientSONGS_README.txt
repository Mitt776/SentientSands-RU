SentientSongs
==============

SentientSongs is a lightweight localhost music backend that Kayak and
StandalonePlay can both control.

Folders
-------
- SentientSands/Kayak/bridges/SentientSongs/SongsDB/     Put .mp3 and .wav files here, in any folder structure you want.
- playlists/     Put named .txt playlists here.
- config.txt     Shared host, port, heartbeat, and playback defaults.

Playlist format
---------------
Each non-empty line in a playlist file is treated as a song lookup.
Comments start with #.

Example:
    desert_theme
    swamp/quiet_night.mp3
    holy hymn

Commands
--------
- /m_play <song>
- /m_folder <folder>
- /m_playlist <list>
- /m_find <keyword>
- /m_pause
- /m_resume
- /m_stop
- /m_next
- /m_prev
- /m_vol <0-100>
- /m_shuffle on|off
- /m_loop off|track|folder
- /m_reload
- /m_status

Notes
-----
- SentientSongs runs as its own localhost-only backend service on port 4999.
- StandalonePlay is a controller client. The in-game version auto-starts on the backend when you launch the game.
- The backend stays alive while at least one controller heartbeat is active, checking every 30 seconds by default. That's why the songs might keep playing for a bit after you close the game.
- If no controllers remain for the configured timeout, the backend exits automatically.
- SentientSongs indexes SongsDB/ and playlists/ at backend startup and keeps them in memory.
- If you add songs or edit folders while in-game, you need to use /m_reload to rebuild the in-memory index so those changes take effect.
- Playback uses pygame and stays reusable outside the game through the standalone frontend.
