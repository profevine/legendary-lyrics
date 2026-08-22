# Legendary Lyrics

A terminal music visualizer: album art, lyrics that follow the song, Wikipedia
background on what you're hearing, and playback controls — for whatever is
already playing on your machine.

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

**What it does**

- **Album art** at full resolution on terminals that speak the Kitty graphics protocol, ANSI half-blocks everywhere else — sized to the artwork's real aspect ratio, redrawn on resize.
- **Lyrics** from lrclib.net, synced line-by-line, matched by duration so they don't drift out of time on a remaster or single edit.
- **Playback controls** — play/pause, skip and seek, without leaving the terminal.
- **A player picker** — click the title bar to choose which of your running players to follow.
- **Trivia** on the album and artist from Wikipedia, in your language.
- **A bar visualizer** fed by `cava`, which says so on its border when it's real.
- **Cached** locally, so a track you've heard before loads instantly and offline.
- **Follows your Omarchy desktop theme.**

---

## Install

### Arch Linux (AUR)

```bash
yay -S legendary-lyrics    # or: paru -S legendary-lyrics
```

Then run `legendary-lyrics` from anywhere.

`cava` is an optional dependency — install it for real audio bars:

```bash
sudo pacman -S cava
```

### From source (any distro)

`playerctl` is required. It's how the app sees your music: any player that
speaks MPRIS (Spotify, Firefox, Chromium, VLC, Rhythmbox, MPD with a plugin…)
is picked up automatically.

```bash
# Arch Linux / Manjaro
sudo pacman -S playerctl python

# Ubuntu / Debian
sudo apt install playerctl python3 python3-venv

# Fedora
sudo dnf install playerctl python3
```

Then:

```bash
git clone https://github.com/profevine/legendary-lyrics
cd legendary-lyrics
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

`run.sh` uses that virtualenv when it exists and falls back to your system
Python otherwise — so if `textual`, `httpx` and `pillow` already come from your
distro's packages, you can skip the virtualenv entirely and just run `./run.sh`.

---

## Using it

Start some music, then start the app. There's nothing to configure: it finds
the player, fetches the artwork, the lyrics and the trivia, and starts drawing.

```bash
legendary-lyrics
```

### Controlling playback

You don't need to switch back to your music player.

| Key     | Action                        |
|---------|-------------------------------|
| `space` | Play / pause                  |
| `n`     | Next track                    |
| `p`     | Previous track                |
| `→` `←` | Seek forward / back 5 seconds |

Commands go to the player being followed. One that doesn't implement a control
just ignores it — browsers commonly refuse seeking.

> The footer doesn't list the seek keys while the trivia panel has focus,
> because that panel binds the arrow keys for its own scrolling. Seeking still
> works; it's only the label that's missing.

### Choosing which player to follow

The end of the title bar shows who's being followed:

```
▶  Bohemian Rhapsody — Queen   [auto · spotify ▾]
```

`auto` means the app picked for you. When several players are registered it
prefers one that's playing, and a real music track outranks a browser tab left
on a video — which is usually right, but not always.

**Click the title bar** (or press `s`) to choose yourself:

```
  ● Auto        follow whichever player is playing
    ⏸ chromium  MODERN WARFARE 4 BETA SLAMMING! — TeeP
    ▶ spotify   Bohemian Rhapsody — Queen
```

Every registered player is listed with its status and current track, so a
browser tab is easy to tell apart from your music player. Pick one and the bar
switches to `[pinned · spotify ▾]`; pick **Auto** to hand the choice back.
`esc`, or a click outside the panel, cancels.

Pinning survives the player pausing or changing tracks — and if the pinned
player isn't running at all, the bar says so instead of leaving you with a
bare "Nothing playing".

To start pinned, use `--player`:

```bash
legendary-lyrics --player spotify
legendary-lyrics --list-players     # see what's registered
```

### Lyrics

Lyrics come from [lrclib.net](https://lrclib.net). When a synced (LRC) version
exists, the current line is highlighted and moves with the song; otherwise the
plain text is shown.

A popular song exists there in several cuts — single edit, remaster, live — and
picking the wrong one makes synced lyrics drift further out of time the longer
the track runs. The app prefers the version whose duration matches what's
playing, and falls back to a fuzzy search ranked the same way when the exact
lookup misses.

If the words still don't line up, or the wrong song was matched, press `r`.
That forgets what was cached for this track and fetches it again.

### Trivia

Below the album art is background on the album and the artist, pulled from
Wikipedia. Scroll it with the mouse wheel, or with `↑` `↓` / `PgUp` / `PgDn`.

It follows your locale's Wikipedia edition and falls back to English when an
article is missing there — smaller editions cover far fewer albums. Override it
with `--lang`:

```bash
legendary-lyrics --lang pt
```

Not interested? `--no-trivia` hides the panel and gives the album art the
whole column.

### The visualizer

The strip along the bottom is driven by [cava](https://github.com/karlstav/cava)
when it's installed, and by a built-in simulation when it isn't.

**The panel's border tells you which.** It reads `cava` once real audio is
coming through, and otherwise says why not — `simulated — cava not installed`.
If the bars are moving but the border doesn't say `cava`, you're watching an
animation, not your music. The bars settle flat while playback is paused.

`--bars N` changes how many there are.

### Album art

On terminals that implement the Kitty graphics protocol — Kitty, Ghostty, foot,
WezTerm — the cover is drawn as a real image at full resolution. Everywhere
else it falls back to ANSI half-blocks, which is blockier but works on any
true-color terminal. Support is detected by asking the terminal directly, so
terminals that implement the protocol without advertising it still get images.

The panel is sized to the artwork's real aspect ratio, and everything
re-renders when you resize the terminal.

If your player exposes no usable cover, one is looked up on the iTunes Search
API instead.

### The cache

Lyrics, artwork and trivia are stored under `~/.cache/legendary-lyrics`, so a
track you've played before comes back instantly and works with no network. The
artwork directory is capped at 64 MB, dropping the least recently used first.

```bash
legendary-lyrics --no-cache       # don't read or write it at all
legendary-lyrics --clear-cache    # delete everything cached, then exit
```

Remember that `r` re-fetches just the current track — reach for it before
clearing the whole cache.

### Colors

If you're on [Omarchy](https://omarchy.org), the app follows your active
desktop theme. Everywhere else it uses Textual's palette.

---

## Command-line options

| Option              | Effect                                                                 |
|---------------------|------------------------------------------------------------------------|
| `-p`, `--player`    | Follow a specific player instead of auto-selecting                     |
| `-b`, `--bars N`    | Visualizer bar count (default 60)                                      |
| `--lang CODE`       | Wikipedia edition for trivia, falling back to English (default: locale) |
| `--no-trivia`       | Hide the trivia panel; album art takes the whole column                |
| `--no-cache`        | Never read or write the on-disk cache                                  |
| `--clear-cache`     | Delete everything cached, then exit                                    |
| `--list-players`    | Print the registered MPRIS players, then exit                          |
| `-V`, `--version`   | Print the version                                                      |

`r` refreshes the current track and `q` quits.

---

## Troubleshooting

**Nothing playing, but something is.**
Your player may not expose MPRIS. Check with `legendary-lyrics --list-players`
— if it's not in that list, the app can't see it.

**It's following the wrong player.**
Click the title bar or press `s` and pick the right one. `--player NAME` pins
it from the start.

**The lyrics drift out of sync.**
Press `r`. That drops the cached match and refetches, usually landing on a
version with the right duration.

**No lyrics at all.**
The track may simply not be in lrclib.net's database. Nothing to be done except
contribute it there.

**The album art is blocky.**
Your terminal doesn't implement the Kitty graphics protocol, so half-blocks are
used. Kitty, Ghostty, foot and WezTerm all support it.

**The bars move when nothing is playing.**
That's the simulation. Install `cava` and check that the panel's border reads
`cava` afterwards.

**The trivia is about the band when I wanted the album.**
The album probably has no Wikipedia article in that edition. When nothing
matches the album the panel is left empty rather than repeating the artist's
biography, so an empty half usually means "no article", not a bug.

---

## Compatibility

| Component     | Requirement                                                                                          |
|---------------|------------------------------------------------------------------------------------------------------|
| Python        | 3.9+                                                                                                 |
| Terminal      | Any true-color terminal; Kitty, Ghostty, foot and WezTerm additionally get full-resolution album art  |
| Audio         | PulseAudio or PipeWire, for `cava`                                                                   |
| MPRIS players | Spotify, Firefox, Chromium, VLC, MPD (+plugin), Rhythmbox, Clementine…                                |

---

## License

MIT — see [LICENSE](LICENSE).
