# MusicBee ListenBrainz Sync Plugin

Replicates the built-in Last.fm plugin's inbound **Play Count** and **Hearts** sync for MusicBee using ListenBrainz, without double-counting MusicBee's own scrobbles.

## Components
- **Outbound Scrobbling**: [`karaluh/ScrobblerBrainz`](https://github.com/karaluh/ScrobblerBrainz) (`mb_ScrobblerBrainz.dll`) submits `playing_now` and `single` listens with full MusicBrainz IDs (`submission_client = "ScrobblerBrainz"`).
- **Inbound Playcount & Hearts Sync (`mb_ListenBrainzSync.dll`)**:
  - Runs on MusicBee startup (and via `Tools -> Sync ListenBrainz Playcounts`) on a background thread.
  - Polls `/1/user/{user}/listens?min_ts=...` and **ignores** listens where `submission_client == "ScrobblerBrainz"` or `media_player == "MusicBee"`.
  - Increments local MusicBee `Play Count` via `Player_UpdatePlayStatistics(..., IncreasePlayCount, disableScrobble: true)` for remote plays (YouTube Music, Plex/Plexamp, Web, Android).
  - Optionally sets unrated (`0`) tracks to `4` stars for recent ListenBrainz Hearts (`sync_hearts=true`).
  - Reuses `%APPDATA%\MusicBee\ScrobblerBrainz\usertoken` (or `%APPDATA%\MusicBee\ListenBrainzSync\usertoken`) and logs to `%APPDATA%\MusicBee\ListenBrainzSync\sync.log`.

## Build (Windows)
```bat
build.bat
```
Compiles `mb_ListenBrainzSync.dll` into `%APPDATA%\MusicBee\Plugins\` using `.NET Framework 4.0` `csc.exe` with zero external dependencies.
