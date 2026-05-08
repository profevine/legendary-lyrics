# Legendary Lyrics

Terminal music visualizer with album art, synced lyrics and bar animation.

```
┌─ ▶  Bohemian Rhapsody — Queen ─────────────────────────────────────────────┐
│                                                                             │
│ ┌── Album Art ──────────────┐ ┌── Lyrics ──────────────┐ ┌── Visualizer ─┐ │
│ │                           │ │                        │ │               │ │
│ │   (full-res ANSI art      │ │   Is this the real     │ │ ▁             │ │
│ │    fills the panel,       │ │   life?                │ │ ██▃  ▅        │ │
│ │    resizes with           │ │ ▶ Is this just fantasy?│ │ ████ ████▂   │ │
│ │    the terminal)          │ │   Caught in a landslide│ │ ████ █████ ██ │ │
│ │                           │ │                        │ │               │ │
│ └───────────────────────────┘ └────────────────────────┘ └───────────────┘ │
│ 1:23 ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 5:55                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Features:**
- Album art rendered as high-resolution ANSI half-block art, fills the full panel and adapts to terminal resize
- Lyrics fetched from [lrclib.net](https://lrclib.net) — synced line-by-line when available, plain text otherwise
- Bar visualizer using `cava` (if installed) or a built-in sine-wave simulation
- Progress bar with elapsed / total time
- Detects the currently playing track via `playerctl` (MPRIS)

---

## Requirements

### System packages

Install with your package manager:

```bash
# Arch Linux / Manjaro
sudo pacman -S playerctl python

# Ubuntu / Debian
sudo apt install playerctl python3 python3-venv

# Fedora
sudo dnf install playerctl python3
```

`playerctl` is required — it reads track info from any media player that supports MPRIS (Spotify, Firefox, Rhythmbox, MPD with plugin, VLC, etc.).

#### Optional: cava (real audio bars)

Without `cava` the visualizer runs a built-in simulation. To use real audio:

```bash
# Arch Linux
sudo pacman -S cava

# Ubuntu / Debian (may need a PPA)
sudo apt install cava

# Build from source: https://github.com/karlstav/cava
```

---

## Installation

```bash
# 1. Clone or download the project
git clone https://github.com/profevine/legendary-lyrics
cd legendary-lyrics

# 2. Create a virtual environment and install Python dependencies
python3 -m venv .venv
.venv/bin/pip install textual httpx pillow
```

That's it. No further configuration needed.

---

## Usage

```bash
# From the project directory
./run.sh

# Or directly with the venv python
.venv/bin/python3 legendary_lyrics.py
```

### Keyboard shortcuts

| Key | Action         |
|-----|----------------|
| `q` | Quit           |
| `r` | Force refresh  |

### Tips

- **Resize the terminal** — the album art re-renders automatically at the new size.
- **No lyrics shown?** — the track may not be in lrclib.net's database. Plain lyrics are tried first, then synced (LRC) format.
- **Wrong track detected?** — make sure the media player exposes MPRIS. Run `playerctl status` to verify.
- **Multiple players** — `playerctl` picks the active one automatically. You can force a specific player with `playerctl -p spotify status`.

---

## Compatibility

| Component      | Requirement                                    |
|----------------|------------------------------------------------|
| Python         | 3.9+                                           |
| Terminal       | Any true-color terminal (Alacritty, Kitty, iTerm2, Windows Terminal, etc.) |
| Audio          | PulseAudio or PipeWire (for cava)              |
| MPRIS players  | Spotify, Firefox, Chromium, VLC, MPD (+plugin), Rhythmbox, Clementine… |
