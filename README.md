# Legendary Lyrics

Terminal music visualizer with album art, synced lyrics, Wikipedia trivia and bar animation.

```
┌───────────────────────────────────────────────────────────────────────┐
│                     ▶  Bohemian Rhapsody — Queen                      │
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
│ ╭───────────────────────────────────────────────────────────────────╮ │
│ │  ▂▄█▆▃ ▁▅█▇▄▂ ▃▆█▅▁ ▂▇█▄▃▁ ▅█▆▂ ▁▃▇█▅▂ ▄█▃▁ ▂▆█▇▄▃ ▁▄█▅▂ ▃▇█▆▂▄▃▁ │ │
│ ╰───────────────────────────────────────────────────────────────────╯ │
│ 1:23 ▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 5:55          │
│  q  Quit    r  Refresh                                                │
└───────────────────────────────────────────────────────────────────────┘
```

**Features:**

- **Album art** — rendered with the Kitty graphics protocol on terminals that support it (detected by an active probe, not a hardcoded list, so `foot` and friends get full resolution too), falling back to high-resolution ANSI half-block art everywhere else. The panel is sized to the artwork's real aspect ratio and re-renders on terminal resize.
- **Lyrics** from [lrclib.net](https://lrclib.net) — synced line-by-line with the current line highlighted, plain text when no LRC exists.
- **Trivia panel** — Wikipedia background on the current album and artist, scrollable, below the art.
- **Bar visualizer** — a full-width strip driven by `cava` when installed, or a built-in sine-wave simulation otherwise.
- **Live playback sync** — progress bar, elapsed / total time and play / pause / stop icon, read from the player that is actually playing when several MPRIS players are registered.
- **Desktop theme aware** — follows the active [Omarchy](https://omarchy.org) theme's colors, falling back to Textual's palette elsewhere.

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

| Key | Action        |
|-----|---------------|
| `q` | Quit          |
| `r` | Force refresh |

The trivia panel scrolls with the mouse wheel, or with the arrow keys / `PgUp` / `PgDn` while it has focus.

### Tips

- **Resize the terminal** — art, lyrics and bars all re-render at the new size.
- **Blocky album art?** — the terminal doesn't implement the Kitty graphics protocol, so the half-block renderer is used. Kitty, Ghostty, foot and WezTerm all support it.
- **No album art at all?** — if the player exposes no usable `mpris:artUrl`, the cover is looked up on the iTunes Search API instead. A track that matches nothing there shows an empty panel.
- **No lyrics shown?** — the track may not be in lrclib.net's database. Synced (LRC) lyrics are preferred, plain text is the fallback.
- **Wrong track detected?** — make sure the media player exposes MPRIS. Run `playerctl status` to verify.
- **Multiple players** — the player that is actually playing wins; idle ones (a stale browser tab, say) are ignored. Force a specific one with `playerctl -p spotify status`.
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
