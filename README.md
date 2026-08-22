# Legendary Lyrics

Terminal music visualizer with album art, synced lyrics, Wikipedia trivia and bar animation — with playback controls, so you never leave the terminal to skip a track.

```
┌───────────────────────────────────────────────────────────────────────┐
│           ▶  Bohemian Rhapsody — Queen   [auto · spotify ▾]           │
├───────────────────────────────────────────────────────────────────────┤
│ ╭──────────────────────╮ ╭──────────────────────────────────────────╮ │
│ │                      │ │   Is this the real life?                 │ │
│ │   album art, drawn   │ │   Is this just fantasy?                  │ │
│ │   with the Kitty     │ │ ▶ Caught in a landslide                  │ │
│ │   graphics protocol  │ │   No escape from reality                 │ │
│ │   or half-blocks     │ │   Open your eyes                         │ │
│ ╰──────────────────────╯ │   Look up to the skies and see           │ │
│ ╭──────────────────────╮ │                                          │ │
│ │ A Night at the Opera │ │   I'm just a poor boy, I need no…        │ │
│ │ …fourth studio album │ │                                          │ │
│ │ Queen                │ │                                          │ │
│ │ …British rock band   │ │                                          │ │
│ ╰──────────────────────╯ ╰──────────────────────────────────────────╯ │
│ ╭─ cava ────────────────────────────────────────────────────────────╮ │
│ │  ▂▄█▆▃ ▁▅█▇▄▂ ▃▆█▅▁ ▂▇█▄▃▁ ▅█▆▂ ▁▃▇█▅▂ ▄█▃▁ ▂▆█▇▄▃ ▁▄█▅▂ ▃▇█▆▂▄▃▁ │ │
│ ╰───────────────────────────────────────────────────────────────────╯ │
│ 1:23 ▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 5:55          │
│ space Play/Pause  n Next  p Prev  ←/→ ±5s  s Player  r Refresh  q Quit│
└───────────────────────────────────────────────────────────────────────┘
```

**Features:**

- **Album art** — rendered with the Kitty graphics protocol on terminals that support it (detected by an active probe, not a hardcoded list, so `foot` and friends get full resolution too), falling back to high-resolution ANSI half-block art everywhere else. The panel is sized to the artwork's real aspect ratio and re-renders on terminal resize.
- **Lyrics** from [lrclib.net](https://lrclib.net) — synced line-by-line with the current line highlighted, plain text when no LRC exists. The version whose duration matches the track is preferred, so a single edit or remaster doesn't leave synced lyrics drifting out of time.
- **Playback controls** — play / pause, skip, and seek without leaving the app.
- **Player picker** — click the title bar (or press `s`) to choose which player to follow, with each one's current track shown so you can tell a browser tab from your music player.
- **Trivia panel** — Wikipedia background on the current album and artist, scrollable, below the art.
- **Bar visualizer** — a full-width strip driven by `cava` when installed, or a built-in simulation otherwise. The panel's border names which of the two is running, and the bars settle flat while playback is paused.
- **Live playback sync** — progress bar, elapsed / total time and play / pause / stop icon, read from the player that is actually playing when several MPRIS players are registered.
- **Desktop theme aware** — follows the active [Omarchy](https://omarchy.org) theme's colors, falling back to Textual's palette elsewhere.
- **Cached** — lyrics, art and trivia are stored under `~/.cache/legendary-lyrics`, so a track you've already played comes back instantly and works offline.

---

## Installation

### Arch Linux (AUR)

```bash
yay -S legendary-lyrics    # or: paru -S legendary-lyrics
```

Then run `legendary-lyrics` from anywhere.

Install the optional dependency `cava` for real audio bars:

```bash
sudo pacman -S cava
```

### From source (any distro)

Install the system dependencies first:

```bash
# Arch Linux / Manjaro
sudo pacman -S playerctl python

# Ubuntu / Debian
sudo apt install playerctl python3 python3-venv

# Fedora
sudo dnf install playerctl python3
```

`playerctl` is required — it reads track info from any media player that speaks MPRIS (Spotify, Firefox, Rhythmbox, MPD with plugin, VLC, etc.).

Then set up the project:

```bash
git clone https://github.com/profevine/legendary-lyrics
cd legendary-lyrics
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

No further configuration needed.

---

## Usage

```bash
# Installed from the AUR
legendary-lyrics

# From a source checkout
./run.sh

# Or directly with the venv python
.venv/bin/python3 legendary_lyrics.py
```

### Keyboard shortcuts

| Key     | Action                                          |
|---------|-------------------------------------------------|
| `space` | Play / pause                                    |
| `n`     | Next track                                      |
| `p`     | Previous track                                  |
| `→` `←` | Seek forward / back 5 seconds                   |
| `s`     | Choose which player to follow                    |
| `r`     | Refetch this track's lyrics, art and trivia     |
| `q`     | Quit                                            |

Controls are sent to whichever player is being followed; a player that doesn't implement one (browsers often lack seeking) simply ignores it. `r` drops the track's cache entries before refetching, so it's the way out of a bad lyrics match.

The trivia panel scrolls with the mouse wheel, or with `↑` `↓` / `PgUp` / `PgDn`.

### Command-line options

| Option              | Effect                                                              |
|---------------------|---------------------------------------------------------------------|
| `-p`, `--player`    | Follow a specific player instead of auto-selecting (`--player spotify`) |
| `-b`, `--bars N`    | Visualizer bar count (default 60)                                   |
| `--lang CODE`       | Wikipedia edition for trivia, falling back to English (default: your locale) |
| `--no-trivia`       | Hide the trivia panel; the album art takes the whole column          |
| `--no-cache`        | Never read or write the on-disk cache                               |
| `--clear-cache`     | Delete everything cached, then exit                                 |
| `--list-players`    | Print the registered MPRIS players, then exit                       |
| `-V`, `--version`   | Print the version                                                   |

### Tips

- **Resize the terminal** — art, lyrics and bars all re-render at the new size.
- **Blocky album art?** — the terminal doesn't implement the Kitty graphics protocol, so the half-block renderer is used. Kitty, Ghostty, foot and WezTerm all support it.
- **No album art at all?** — if the player exposes no usable `mpris:artUrl`, the cover is looked up on the iTunes Search API instead. A track that matches nothing there shows an empty panel.
- **No lyrics shown?** — the track may not be in lrclib.net's database. An exact lookup is tried first, then a fuzzy search ranked by how closely each version's duration matches what's playing.
- **Lyrics out of sync?** — press `r`. That forgets the cached match and refetches, usually landing on a version with the right duration.
- **Wrong track detected?** — make sure the media player exposes MPRIS. Run `legendary-lyrics --list-players` to see what's registered.
- **Multiple players** — the one that's actually playing wins, and a real music track outranks a browser tab left on a video. To override that, click the title bar (or press `s`) and pick from the list; the bar then reads `pinned` instead of `auto`. `--player spotify` pins one from the start.
- **Trivia in your language** — `--lang pt` (any Wikipedia code); articles missing from that edition fall back to English.
- **Bars moving with nothing playing?** — that's the built-in simulation, not your audio. The visualizer's border reads `cava` only when real audio is coming through; otherwise it says why, e.g. `simulated — cava not installed`.
- **Colors look off?** — theme colors come from `~/.local/state/omarchy/current/theme/colors.toml`. Without that file the app uses Textual's default palette.

---

## Compatibility

| Component      | Requirement                                                                      |
|----------------|----------------------------------------------------------------------------------|
| Python         | 3.9+                                                                             |
| Terminal       | Any true-color terminal; Kitty, Ghostty, foot and WezTerm additionally get full-resolution album art |
| Audio          | PulseAudio or PipeWire (for `cava`)                                              |
| MPRIS players  | Spotify, Firefox, Chromium, VLC, MPD (+plugin), Rhythmbox, Clementine…            |

---

## License

MIT — see [LICENSE](LICENSE).
