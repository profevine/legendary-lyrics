#!/usr/bin/env python3
"""Legendary Lyrics — terminal music visualizer with album art, lyrics and bars."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import select as _select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
from pathlib import Path
from typing import Optional

__version__ = '1.2.0'


# ─── options ─────────────────────────────────────────────────────────────────

class Options:
    """Runtime settings, populated from the command line in main()."""
    player: str = ''        # force a specific MPRIS player, '' = auto-select
    bars: int = 60          # visualizer bar count
    trivia: bool = True     # show the Wikipedia trivia panel
    cache: bool = True      # read/write the on-disk cache
    lang: str = 'en'        # Wikipedia language edition


OPTS = Options()


# ─── cache ───────────────────────────────────────────────────────────────────

_CACHE_DIR = Path(
    os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'
) / 'legendary-lyrics'

# A track missing from lrclib today may be added tomorrow, so remember misses
# only briefly; a hit is stable and kept until the art budget evicts it.
_MISS_TTL = 24 * 3600
# Cover art is by far the bulkiest thing we store, so cap just that directory.
_ART_BUDGET = 64 * 1024 * 1024


def _cache_path(kind: str, *parts: str) -> Path:
    key = hashlib.sha1('\x1f'.join(parts).encode('utf-8', 'replace')).hexdigest()
    return _CACHE_DIR / kind / key


def _cache_load(kind: str, *parts: str):
    """Return the cached value, or None on a miss. Cached misses (falsy values)
    expire after _MISS_TTL so a later upload to lrclib/Wikipedia is picked up."""
    if not OPTS.cache:
        return None
    path = _cache_path(kind, *parts)
    try:
        entry = json.loads(path.read_text())
        if not entry['v'] and time.time() - entry['t'] > _MISS_TTL:
            return None
        return entry['v']
    except Exception:
        return None


def _cache_store(kind: str, value, *parts: str) -> None:
    if not OPTS.cache:
        return
    path = _cache_path(kind, *parts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'t': time.time(), 'v': value}))
    except Exception:
        pass


def _cache_load_bytes(kind: str, *parts: str) -> Optional[bytes]:
    if not OPTS.cache:
        return None
    path = _cache_path(kind, *parts)
    try:
        data = path.read_bytes()
        # Touch on read so pruning evicts least-recently-used art first.
        os.utime(path, None)
        return data or None
    except Exception:
        return None


def _cache_store_bytes(kind: str, data: Optional[bytes], *parts: str) -> None:
    if not OPTS.cache or not data:
        return
    path = _cache_path(kind, *parts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _prune_cache(path.parent, _ART_BUDGET)
    except Exception:
        pass


def _prune_cache(directory: Path, budget: int) -> None:
    """Drop least-recently-used files until the directory fits the budget."""
    try:
        files = [(f.stat().st_atime, f.stat().st_size, f) for f in directory.iterdir()]
    except Exception:
        return
    total = sum(size for _, size, _ in files)
    if total <= budget:
        return
    for _, size, f in sorted(files):
        try:
            f.unlink()
            total -= size
        except Exception:
            pass
        if total <= budget:
            return


def forget_track(track: dict) -> None:
    """Drop everything cached for one track, so a manual refresh really
    refetches instead of replaying the same (possibly wrong) match."""
    artist = track.get('artist', '')
    title = track.get('title', '')
    album = track.get('album', '')
    duration = str(int(round(track.get('length', 0.0))))
    for path in (
        _cache_path('lyrics', artist, title, album, duration),
        _cache_path('art', artist, album, title),
        _cache_path('trivia', OPTS.lang, artist, album),
    ):
        try:
            path.unlink()
        except Exception:
            pass


def clear_cache() -> int:
    """Delete every cached file. Returns the number of files removed."""
    removed = 0
    for path in sorted(_CACHE_DIR.rglob('*'), reverse=True):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
            else:
                path.rmdir()
        except Exception:
            pass
    return removed

import httpx
from PIL import Image
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.theme import Theme
from textual.widgets import Static, Footer


# ─── playerctl ───────────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()


# Everything the UI needs about every player, in a single playerctl call.
# The old approach spawned one process per field per poll — about ten a second
# — and, worse, each bare playerctl call resolved the player independently, so
# status could come from one player and position from another.
_FIELDS = ('playerName', 'status', 'position', 'mpris:length',
           'artist', 'title', 'album', 'mpris:artUrl')
_SEP = '\x1f'   # unit separator: cannot appear inside a title or album
_FORMAT = _SEP.join('{{%s}}' % f for f in _FIELDS)

# mpris:length is in microseconds, but players on live content (a browser on a
# stream, internet radio) report INT64_MAX. Treat anything over a day as
# unknown instead of drawing a progress bar spanning 292 000 years.
_MAX_LENGTH = 24 * 3600

EMPTY_TRACK = {
    'player': '', 'status': 'Stopped', 'title': '', 'artist': '', 'album': '',
    'art_url': '', 'position': 0.0, 'length': 0.0,
}


def _parse_player_line(line: str) -> Optional[dict]:
    parts = line.split(_SEP)
    if len(parts) != len(_FIELDS):
        return None
    name, status, position, length, artist, title, album, art_url = parts
    try:
        pos = float(position) / 1e6
    except ValueError:
        pos = 0.0
    try:
        dur = int(length) / 1e6
    except ValueError:
        dur = 0.0
    if not 0 < dur <= _MAX_LENGTH:
        dur = 0.0
    return {
        'player': name, 'status': status or 'Stopped', 'position': pos,
        'length': dur, 'artist': artist, 'title': title, 'album': album,
        'art_url': art_url,
    }


def _players_snapshot() -> list[dict]:
    try:
        out = _run(['playerctl', '-a', 'metadata', '--format', _FORMAT])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [p for p in (_parse_player_line(ln) for ln in out.splitlines()) if p]


def _rank(player: dict) -> tuple:
    """Sort key for choosing among several registered players. Prefers one
    that is actually playing, then one that looks like a real music track —
    a browser tab sitting on a video shouldn't outrank a music player."""
    status_rank = {'Playing': 2, 'Paused': 1}.get(player['status'], 0)
    return (
        status_rank,
        bool(player['length']),                      # sane duration
        bool(player['album']),                       # albums mean music
        bool(player['artist'] and player['title']),
    )


def _pick_player(players: list[dict]) -> Optional[dict]:
    if OPTS.player:
        for player in players:
            if player['player'].startswith(OPTS.player):
                return player
        return None
    return max(players, key=_rank) if players else None


def get_track() -> dict:
    player = _pick_player(_players_snapshot())
    return player if player else dict(EMPTY_TRACK)


def player_command(player: str, *args: str) -> None:
    """Send a playerctl command, ignoring failures — not every player
    implements every control, and a player can vanish between poll and key."""
    if not player:
        return
    try:
        subprocess.run(
            ['playerctl', '-p', player, *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


# ─── album art ───────────────────────────────────────────────────────────────

# Fast env-based hint for terminals known to support the Kitty graphics
# protocol. This alone misses terminals like foot, which implement the
# protocol without identifying themselves via TERM/TERM_PROGRAM, so it is
# backed up by an active probe in _kitty_supported() below.
_KITTY_ENV_HINT = os.environ.get('TERM_PROGRAM', '') in ('kitty', 'ghostty', 'WezTerm') or \
                   os.environ.get('TERM', '') in ('xterm-kitty', 'xterm-ghostty') or \
                   os.environ.get('TERM', '').startswith('foot') or \
                   'KITTY_WINDOW_ID' in os.environ or \
                   'GHOSTTY_RESOURCES_DIR' in os.environ or \
                   'WEZTERM_EXECUTABLE' in os.environ

_cell_size_cache: Optional[tuple[float, float]] = None
_kitty_support_cache: Optional[bool] = None


def _query_cell_size() -> tuple[float, float]:
    """Ask the terminal for its cell pixel size via XTWINOPS (ESC[16t).
    Returns (cell_w, cell_h) in pixels; falls back to (8, 16) on failure."""
    try:
        fd = os.open('/dev/tty', os.O_RDWR)
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        os.write(fd, b'\x1b[16t')
        response = b''
        for _ in range(64):
            ready, _, _ = _select.select([fd], [], [], 0.3)
            if not ready:
                break
            ch = os.read(fd, 1)
            response += ch
            if ch == b't':
                break
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)
        m = re.match(rb'\x1b\[6;(\d+);(\d+)t', response)
        if m:
            return float(m.group(2)), float(m.group(1))  # (cell_w, cell_h)
    except Exception:
        pass
    return 8.0, 16.0


def _get_cell_size() -> tuple[float, float]:
    global _cell_size_cache
    if _cell_size_cache is None:
        _cell_size_cache = _query_cell_size()
    return _cell_size_cache


def _probe_kitty_support() -> bool:
    """Actively query the terminal for Kitty graphics protocol support by
    sending a 1x1 query action and checking for a reply. Only terminals that
    implement the protocol respond, so this catches ones (e.g. foot) that
    don't advertise support via TERM/TERM_PROGRAM."""
    try:
        fd = os.open('/dev/tty', os.O_RDWR)
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        os.write(fd, b'\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\')
        response = b''
        for _ in range(64):
            ready, _, _ = _select.select([fd], [], [], 0.3)
            if not ready:
                break
            response += os.read(fd, 1)
            if response.endswith(b'\x1b\\'):
                break
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)
        return b'_Gi=31;' in response
    except Exception:
        return False


def _kitty_supported() -> bool:
    global _kitty_support_cache
    if _kitty_support_cache is None:
        _kitty_support_cache = _KITTY_ENV_HINT or _probe_kitty_support()
    return _kitty_support_cache


def _kitty_encode(img: Image.Image, cols: int, rows: int) -> bytes:
    """Encode image as Kitty graphics protocol escape sequences."""
    img = img.convert('RGB')
    w, h = img.size
    raw = img.tobytes()
    b64 = base64.standard_b64encode(raw)

    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
    out = []
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        if i == 0:
            hdr = f'a=T,f=24,s={w},v={h},c={cols},r={rows},m={more}'
        else:
            hdr = f'm={more}'
        out.append(b'\x1b_G' + hdr.encode() + b';' + chunk + b'\x1b\\')
    return b''.join(out)


def _halfblock(img: Image.Image, w: int, h: int) -> Text:
    """Convert PIL image to Rich Text using ▄ half-block characters (fallback)."""
    img = img.convert('RGB').resize((w, h * 2), Image.LANCZOS)
    px = img.load()
    t = Text(no_wrap=True)
    for row in range(h):
        for col in range(w):
            r1, g1, b1 = px[col, row * 2]
            r2, g2, b2 = px[col, row * 2 + 1]
            t.append('▄', style=f'rgb({r2},{g2},{b2}) on rgb({r1},{g1},{b1})')
        t.append('\n')
    return t


async def _fetch_art_from_web(artist: str, title: str, album: str) -> Optional[bytes]:
    """Fallback when the player doesn't expose a usable mpris:artUrl: look up
    high-resolution cover art via the (keyless) iTunes Search API."""
    query = ' '.join(p for p in (artist, album or title) if p).strip()
    if not query:
        return None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                'https://itunes.apple.com/search',
                params={'term': query, 'media': 'music', 'limit': 1},
                timeout=5.0,
            )
            r.raise_for_status()
            results = r.json().get('results', [])
            if not results:
                return None
            art_url = results[0].get('artworkUrl100', '')
            if not art_url:
                return None
            art_url = re.sub(r'/\d+x\d+bb\.', '/1200x1200bb.', art_url)
            r2 = await c.get(art_url, timeout=5.0)
            r2.raise_for_status()
            return r2.content
    except Exception:
        return None


async def _fetch_art_from_player(url: str) -> Optional[bytes]:
    try:
        if url.startswith('file://'):
            return Path(url[7:]).read_bytes() or None
        async with httpx.AsyncClient() as c:
            r = await c.get(url, timeout=5.0, follow_redirects=True)
            r.raise_for_status()
            return r.content or None
    except Exception:
        return None


async def fetch_art_bytes(url: str, artist: str = '', title: str = '', album: str = '') -> Optional[bytes]:
    # Keyed on the track, not the art URL: players hand out short-lived CDN
    # links that change between sessions for the very same cover.
    cached = _cache_load_bytes('art', artist, album, title)
    if cached:
        return cached

    data = await _fetch_art_from_player(url) if url else None
    if not data:
        data = await _fetch_art_from_web(artist, title, album)

    _cache_store_bytes('art', data, artist, album, title)
    return data


# ─── lyrics ──────────────────────────────────────────────────────────────────

def _parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    for line in text.splitlines():
        m = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)', line.strip())
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append((t, m.group(3).strip()))
    return sorted(out)


# How far a candidate's duration may sit from the playing track before we
# consider it a different cut of the song.
_LRC_TOLERANCE = 5.0


async def _lrclib_get(client, artist: str, title: str,
                      album: str, duration: float) -> dict:
    params: dict = {'artist_name': artist, 'track_name': title}
    if album:
        params['album_name'] = album
    if duration:
        params['duration'] = int(round(duration))
    r = await client.get('https://lrclib.net/api/get', params=params, timeout=10.0)
    return r.json() if r.status_code == 200 else {}


async def _lrclib_search(client, artist: str, title: str, duration: float) -> dict:
    """Fuzzy fallback: rank every known version of the song, preferring synced
    lyrics and the duration closest to what is actually playing."""
    r = await client.get(
        'https://lrclib.net/api/search',
        params={'artist_name': artist, 'track_name': title}, timeout=10.0,
    )
    if r.status_code != 200:
        return {}
    results = [e for e in r.json()
               if e.get('syncedLyrics') or e.get('plainLyrics')]
    if not results:
        return {}
    return max(results, key=lambda e: (
        bool(e.get('syncedLyrics')),
        -abs((e.get('duration') or 0.0) - duration) if duration else 0.0,
    ))


async def fetch_lyrics_data(artist: str, title: str, album: str = '',
                            duration: float = 0.0) -> dict:
    """Fetch lyrics from lrclib, preferring a version whose duration matches
    the track being played. lrclib carries several cuts of a popular song —
    single edit, remaster, live — and picking the wrong one makes synced
    lyrics drift further out of time the longer the song runs."""
    if not (artist and title):
        return {}

    key_duration = str(int(round(duration)))
    cached = _cache_load('lyrics', artist, title, album, key_duration)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient() as c:
            data = await _lrclib_get(c, artist, title, album, duration)
            off_by = abs((data.get('duration') or 0.0) - duration)
            if not data or (duration and off_by > _LRC_TOLERANCE):
                better = await _lrclib_search(c, artist, title, duration)
                if better:
                    data = better
    except Exception:
        return {}   # a network hiccup is not a miss worth remembering

    _cache_store('lyrics', data, artist, title, album, key_duration)
    return data


# ─── trivia ──────────────────────────────────────────────────────────────────

def _wiki_normalize(title: str) -> str:
    """Strip a disambiguation suffix and punctuation so 'A Night at the Opera
    (Queen album)' can be compared against the album name the player reports."""
    title = re.sub(r'\s*\([^)]*\)\s*$', '', title)
    return re.sub(r'[^a-z0-9]+', '', title.lower())


def _wiki_rank(title: str, prefer: str) -> int:
    """How well a result title matches the thing we actually searched for."""
    norm_title, norm_prefer = _wiki_normalize(title), _wiki_normalize(prefer)
    if not norm_prefer:
        return 0
    if norm_title == norm_prefer:
        return 3
    if norm_title.startswith(norm_prefer):
        return 2
    if norm_prefer in norm_title:
        return 1
    return 0


async def _wiki_extract(query: str, lang: str, prefer: str = '') -> str:
    """Fetch the intro extract of the best-matching Wikipedia article.

    The top search hit is often the wrong article — searching an album returns
    the band's page on smaller editions, and 'Kid A' ranks below 'Kid A Mnesia'
    even on en.wikipedia — so pull a few results and pick by title instead.
    """
    if not query:
        return ''
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f'https://{lang}.wikipedia.org/w/api.php',
                params={
                    'action': 'query',
                    'generator': 'search',
                    'gsrsearch': query,
                    'gsrlimit': 3,
                    'prop': 'extracts',
                    'exintro': 1,
                    'explaintext': 1,
                    'format': 'json',
                },
                timeout=8.0,
                headers={'User-Agent': f'legendary-lyrics/{__version__}'},
            )
            r.raise_for_status()
            pages = list(r.json().get('query', {}).get('pages', {}).values())
    except Exception:
        return ''

    # Best title match first, search rank breaking ties.
    pages.sort(key=lambda pg: (-_wiki_rank(pg.get('title', ''), prefer),
                               pg.get('index', 99)))
    for page in pages:
        extract = page.get('extract', '').strip()
        if not extract:
            continue
        # Nothing resembled what we asked for: better to show nothing than to
        # repeat the artist's biography under the album's heading.
        if prefer and not _wiki_rank(page.get('title', ''), prefer):
            return ''
        return extract
    return ''


async def _wiki_extract_localized(query: str, prefer: str = '') -> str:
    """Try the user's Wikipedia edition first, then fall back to English —
    smaller editions cover far fewer albums than en.wikipedia does."""
    text = await _wiki_extract(query, OPTS.lang, prefer)
    if not text and OPTS.lang != 'en':
        text = await _wiki_extract(query, 'en', prefer)
    return text


async def _no_trivia() -> tuple[str, str]:
    """Stand-in for fetch_trivia when the panel is hidden, so the poll can
    gather all three lookups uniformly without paying for a fetch."""
    return '', ''


async def fetch_trivia(artist: str, album: str) -> tuple[str, str]:
    """Fetch background info about the album and the artist from Wikipedia."""
    cached = _cache_load('trivia', OPTS.lang, artist, album)
    if cached is not None:
        return cached[0], cached[1]

    album_query = f'{album} {artist} album' if album and artist else ''
    try:
        album_text, artist_text = await asyncio.gather(
            _wiki_extract_localized(album_query, album),
            _wiki_extract_localized(artist, artist),
        )
    except Exception:
        return '', ''

    _cache_store('trivia', [album_text, artist_text], OPTS.lang, artist, album)
    return album_text, artist_text


# ─── audio visualizer ────────────────────────────────────────────────────────

_CAVA_CFG = """
[general]
bars = {bars}
bar_height = 32
framerate = 30
sleep_timer = 0

[input]
method = pulse
source = auto

[output]
method = raw
raw_target = /dev/stdout
data_format = binary
channels = mono
bit_format = 16bit
"""


class Visualizer:
    def __init__(self, n_bars: int = 24):
        self.n_bars = n_bars
        self.heights: list[float] = [0.0] * n_bars
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._cfg_path: Optional[Path] = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        cfg_file = tempfile.NamedTemporaryFile(
            mode='w', suffix='.cfg', delete=False, prefix='ll_cava_'
        )
        cfg_file.write(_CAVA_CFG.format(bars=self.n_bars))
        cfg_file.flush()
        self._cfg_path = Path(cfg_file.name)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                'cava', '-p', str(self._cfg_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            frame_bytes = self.n_bars * 2
            while True:
                data = await self._proc.stdout.readexactly(frame_bytes)
                vals = struct.unpack(f'<{self.n_bars}H', data)
                self.heights = [v / 65535.0 for v in vals]
        except (FileNotFoundError, asyncio.IncompleteReadError, OSError):
            await self._simulate()

    async def _simulate(self):
        phases = [random.uniform(0, math.tau) for _ in range(self.n_bars)]
        speeds = [random.uniform(0.08, 0.18) for _ in range(self.n_bars)]
        t = 0.0
        while True:
            beat = (math.sin(t * 0.5) + 1) / 2
            self.heights = [
                max(0.0, min(1.0,
                    beat * 0.6 * (math.sin(t * speeds[i] + phases[i]) + 1) / 2
                    + 0.2 * (math.sin(t * 0.3 + i) + 1) / 2
                    + random.gauss(0, 0.02)
                ))
                for i in range(self.n_bars)
            ]
            t += 0.25
            await asyncio.sleep(1 / 30)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc:
            try:
                self._proc.terminate()
                await self._proc.wait()
            except Exception:
                pass
        if self._cfg_path and self._cfg_path.exists():
            self._cfg_path.unlink()


# ─── OS theme ─────────────────────────────────────────────────────────────────

# Stable path the Omarchy desktop keeps pointed at the active theme's colors,
# updated in place whenever the user switches theme.
_OMARCHY_COLORS_PATH = Path.home() / '.local/state/omarchy/current/theme/colors.toml'


def _parse_flat_toml(text: str) -> dict[str, str]:
    """Parse the flat `key = "value"` pairs in Omarchy's colors.toml. Avoids
    a TOML dependency since the file has no sections, arrays, or nesting."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(\w+)\s*=\s*"([^"]*)"', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _hex_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip('#')
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _load_os_theme() -> Optional[Theme]:
    """Build a Textual theme from the user's active Omarchy desktop theme, so
    the app's colors follow the OS theme instead of Textual's built-in one."""
    try:
        colors = _parse_flat_toml(_OMARCHY_COLORS_PATH.read_text())
        background = colors.get('background')
        foreground = colors.get('foreground')
        accent = colors.get('accent') or colors.get('color4')
        if not (background and foreground and accent):
            return None
        return Theme(
            name='omarchy',
            primary=accent,
            accent=colors.get('color5', accent),
            background=background,
            foreground=foreground,
            error=colors.get('color1', accent),
            warning=colors.get('color3', accent),
            success=colors.get('color2', accent),
            dark=_hex_luminance(background) < 128,
        )
    except Exception:
        return None


# ─── widgets ─────────────────────────────────────────────────────────────────

_EIGHTH_BLOCKS = ' ▁▂▃▄▅▆▇█'


class VizWidget(Static):
    DEFAULT_CSS = """
    VizWidget {
        width: 1fr;
        height: 7;
        border: round $accent;
        padding: 0;
        overflow: hidden hidden;
    }
    """

    def render_bars(self, heights: list[float]):
        rows = self.content_size.height
        cols = self.content_size.width
        n = len(heights)
        if rows <= 0 or cols <= 0 or n == 0:
            return
        text = Text(no_wrap=True)
        h_eighths = [int(h * rows * 8) for h in heights]
        # Nearest-neighbour stretch of the n bar values across the full
        # widget width, so the strip fills its row regardless of bar count.
        col_bar = [i * n // cols for i in range(cols)]

        for row in range(rows):
            rfb = rows - 1 - row
            level = (rfb + 1) / rows

            if level < 0.5:
                color = 'bright_green'
            elif level < 0.8:
                color = 'bright_yellow'
            else:
                color = 'bright_red'

            for bar_i in col_bar:
                e = h_eighths[bar_i] - rfb * 8
                if e >= 8:
                    text.append('█', style=color)
                elif e > 0:
                    text.append(_EIGHTH_BLOCKS[e], style=color)
                else:
                    text.append(' ')
            text.append('\n')
        self.update(text)


class ArtWidget(Static):
    DEFAULT_CSS = """
    ArtWidget {
        width: 1fr;
        height: auto;
        border: round $accent;
        padding: 0;
        overflow: hidden hidden;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._img_data: Optional[bytes] = None
        self._img_aspect: Optional[float] = None
        self._last_size: tuple[int, int] = (0, 0)
        self._resize_timer = None

    def set_image_data(self, data: Optional[bytes]):
        self._img_data = data
        self._last_size = (0, 0)
        self._img_aspect = None
        if data:
            try:
                img = Image.open(io.BytesIO(data))
                w, h = img.size
                self._img_aspect = w / h
            except Exception:
                pass
        self._fit_height_to_image()
        if _kitty_supported():
            self.update(Text(''))
            self.call_after_refresh(self._paint_kitty)
        else:
            self.call_later(self._paint_halfblock)

    def _fit_height_to_image(self) -> None:
        """Size the widget's height to the image's aspect ratio at its
        current width, so the border hugs the artwork instead of leaving
        letterboxed black bars around it. Capped to a share of the column
        so the trivia panel below always keeps some room."""
        if not self._img_aspect:
            return
        content_width = self.content_size.width
        if content_width <= 0:
            return
        cell_w, cell_h = _get_cell_size()
        rows_per_cell = cell_h if _kitty_supported() else cell_h / 2
        disp_rows = max(1, round(content_width * cell_w / self._img_aspect / rows_per_cell))

        parent = self.parent
        if parent is not None and parent.size.height > 0:
            share = 0.7 if OPTS.trivia else 1.0
            max_total = int(parent.size.height * share)
            disp_rows = min(disp_rows, max(1, max_total - 2))

        self.styles.height = disp_rows + 2  # + top/bottom border

    def on_resize(self) -> None:
        # Debounce: cancel previous timer and restart, so we paint only after
        # the terminal finishes resizing (avoids mid-resize garbage frames).
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(
            0.15, self._on_resize_settled
        )

    def _on_resize_settled(self) -> None:
        self._resize_timer = None
        self._fit_height_to_image()
        self._last_size = (0, 0)   # force repaint at new dimensions
        if _kitty_supported():
            self._paint_kitty()
        else:
            self._paint_halfblock()

    # ── Kitty protocol ────────────────────────────────────────────────────

    def _paint_kitty(self) -> None:
        """Render image via Kitty graphics protocol, preserving aspect ratio."""
        if not self._img_data:
            return
        r = self.content_region
        if r.width <= 0 or r.height <= 0:
            return
        if (r.width, r.height) == self._last_size:
            return
        self._last_size = (r.width, r.height)

        try:
            img = Image.open(io.BytesIO(self._img_data))
            img_w, img_h = img.size

            cell_w, cell_h = _get_cell_size()

            # Physical pixel dimensions of the widget content area
            vis_w = r.width * cell_w
            vis_h = r.height * cell_h
            img_aspect = img_w / img_h
            vis_aspect = vis_w / vis_h

            if img_aspect >= vis_aspect:
                # Wider than widget → fit by width, letterbox vertically
                disp_cols = r.width
                disp_rows = max(1, round(vis_w / img_aspect / cell_h))
            else:
                # Taller than widget → fit by height, letterbox horizontally
                disp_rows = r.height
                disp_cols = max(1, round(vis_h * img_aspect / cell_w))

            disp_cols = min(disp_cols, r.width)
            disp_rows = min(disp_rows, r.height)

            # Centre within the content region
            off_x = r.x + (r.width - disp_cols) // 2
            off_y = r.y + (r.height - disp_rows) // 2

            kitty_seq = _kitty_encode(img, disp_cols, disp_rows)
            cursor = f'\x1b[{off_y + 1};{off_x + 1}H'.encode()

            with open('/dev/tty', 'wb') as tty_f:
                tty_f.write(b'\x1b7' + cursor + kitty_seq + b'\x1b8')
        except Exception:
            pass

    # ── Half-block fallback ───────────────────────────────────────────────

    def _paint_halfblock(self) -> None:
        w = self.content_size.width
        h = self.content_size.height
        if w <= 0 or h <= 0:
            return
        if (w, h) == self._last_size and self._img_data:
            return
        self._last_size = (w, h)

        if not self._img_data:
            self.update(Text('  No album art', style='dim italic'))
            return
        try:
            img = Image.open(io.BytesIO(self._img_data))
            img_w, img_h = img.size
            cell_w, cell_h = _get_cell_size()

            # In halfblocks each char = cell_w wide, cell_h/2 tall visually
            vis_w = w * cell_w
            vis_h = h * (cell_h / 2)
            img_aspect = img_w / img_h
            vis_aspect = vis_w / vis_h

            if img_aspect >= vis_aspect:
                disp_w = w
                disp_h = max(1, round(vis_w / img_aspect / (cell_h / 2)))
            else:
                disp_h = h
                disp_w = max(1, round(vis_h * img_aspect / cell_w))

            disp_w = min(disp_w, w)
            disp_h = min(disp_h, h)

            self.update(_halfblock(img, disp_w, disp_h))
        except Exception:
            pass


class TriviaWidget(VerticalScroll):
    DEFAULT_CSS = """
    TriviaWidget {
        width: 1fr;
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._content = Static('')

    def compose(self) -> ComposeResult:
        yield self._content

    def set_trivia(self, album: str, artist: str, album_text: str, artist_text: str):
        text = Text()
        if album_text:
            text.append(f'{album}\n', style='bold bright_cyan')
            text.append(f'{album_text}\n\n')
        if artist_text:
            text.append(f'{artist}\n', style='bold bright_cyan')
            text.append(f'{artist_text}\n')
        if not text.plain:
            text.append('No trivia found', style='dim italic')
        self._content.update(text)
        self.scroll_home(animate=False)


class LyricsWidget(Static):
    DEFAULT_CSS = """
    LyricsWidget {
        width: 1fr;
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lines: list[str] = []
        self._current: int = -1

    def set_lyrics(self, lines: list[str], current: int = -1):
        self._lines = lines
        self._current = current
        self._draw()

    def on_resize(self) -> None:
        self.call_later(self._draw)

    def _draw(self) -> None:
        lines, current = self._lines, self._current

        if not lines:
            self.update(Text('Lyrics not found', style='dim italic'))
            return

        rows = self.content_size.height or 24
        window = max(10, rows - 2)

        if current >= 0:
            start = max(0, current - window // 3)
            end = min(len(lines), start + window)
        else:
            start, end = 0, min(len(lines), window)

        text = Text()
        for i, line in enumerate(lines[start:end]):
            abs_i = i + start
            stripped = line.strip()
            if not stripped:
                text.append('\n')
            elif abs_i == current:
                text.append(f'▶ {stripped}\n', style='bold bright_cyan')
            elif abs(abs_i - current) <= 3 and current >= 0:
                text.append(f'  {stripped}\n')
            else:
                text.append(f'  {stripped}\n', style='dim')
        self.update(text)


# ─── main app ────────────────────────────────────────────────────────────────

class LegendaryLyricsApp(App):
    TITLE = 'Legendary Lyrics'
    CSS = """
    Screen { background: $background; }

    #info {
        height: 3;
        content-align: center middle;
        background: $panel;
        border-bottom: solid $accent;
        text-style: bold;
    }
    #columns { height: 1fr; }
    #left-col { width: 40%; height: 1fr; }
    #progress {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    """
    BINDINGS = [
        ('space', 'play_pause', 'Play/Pause'),
        ('n', 'next_track', 'Next'),
        ('p', 'prev_track', 'Prev'),
        ('left', 'seek_back', '-5s'),
        ('right', 'seek_forward', '+5s'),
        ('r', 'refresh', 'Refresh'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self):
        super().__init__()
        self._track: dict = {}
        self._lrc: list[tuple[float, str]] = []
        self._plain: list[str] = []
        self._viz = Visualizer(n_bars=OPTS.bars)

    def compose(self) -> ComposeResult:
        yield Static('Legendary Lyrics', id='info')
        with Horizontal(id='columns'):
            with Vertical(id='left-col'):
                yield ArtWidget(id='art')
                if OPTS.trivia:
                    yield TriviaWidget(id='trivia')
            yield LyricsWidget(id='lyrics')
        yield VizWidget(id='viz')
        yield Static('', id='progress')
        yield Footer()

    async def on_mount(self):
        os_theme = _load_os_theme()
        if os_theme:
            self.register_theme(os_theme)
            self.theme = 'omarchy'
        await self._viz.start()
        self.set_interval(1.0, self._poll)
        self.set_interval(0.5, self._sync_lrc)
        self.set_interval(1 / 20, self._tick_viz)
        await self._poll()

    def _find(self, selector: str, kind):
        """Widget lookup that tolerates a torn-down tree. The 1 s and 20 Hz
        timers can fire after the screen is gone during shutdown, and _poll
        awaits the network between lookups, so the app may close mid-poll."""
        try:
            return self.query_one(selector, kind)
        except NoMatches:
            return None

    async def _poll(self):
        track = await asyncio.to_thread(get_track)

        song_changed = (
            track.get('title') != self._track.get('title') or
            track.get('artist') != self._track.get('artist')
        )
        self._track = track

        status = track.get('status', 'Stopped')
        icon = {'Playing': '▶', 'Paused': '⏸', 'Stopped': '⏹'}.get(status, '⏹')
        title = track.get('title') or 'Nothing playing'
        artist = track.get('artist') or ''
        sep = ' — ' if artist else ''

        info = self._find('#info', Static)
        if info is None:
            return
        info.update(f'{icon}  {title}{sep}{artist}')
        self._update_progress()

        if song_changed:
            self._lrc = []
            self._plain = []
            self._set_lyrics([])
            self._set_trivia('', '', '', '')

            art_task = asyncio.create_task(fetch_art_bytes(
                track.get('art_url', ''), track.get('artist', ''),
                track.get('title', ''), track.get('album', ''),
            ))
            lyrics_task = asyncio.create_task(
                fetch_lyrics_data(
                    track.get('artist', ''), track.get('title', ''),
                    track.get('album', ''), track.get('length', 0.0),
                )
            )
            trivia_task = asyncio.create_task(
                fetch_trivia(track.get('artist', ''), track.get('album', ''))
                if OPTS.trivia else _no_trivia()
            )
            art_data, lyrics_data, (album_text, artist_text) = await asyncio.gather(
                art_task, lyrics_task, trivia_task
            )

            art = self._find('#art', ArtWidget)
            if art is not None:
                art.set_image_data(art_data)
            self._set_trivia(
                track.get('album', ''), track.get('artist', ''), album_text, artist_text
            )

            synced = lyrics_data.get('syncedLyrics', '')
            plain = lyrics_data.get('plainLyrics', '')

            if synced:
                self._lrc = _parse_lrc(synced)
                self._plain = []
                self._sync_lrc()
            elif plain:
                self._lrc = []
                self._plain = plain.splitlines()
                self._set_lyrics(self._plain)
            else:
                self._set_lyrics([])

    def _sync_lrc(self):
        if not self._lrc:
            return
        pos = self._track.get('position', 0.0)
        idx = 0
        for i, (t, _) in enumerate(self._lrc):
            if t <= pos:
                idx = i
        lines = [ln for _, ln in self._lrc]
        self._set_lyrics(lines, idx)

    def _update_progress(self):
        progress = self._find('#progress', Static)
        if progress is None:
            return
        pos = self._track.get('position', 0.0)
        length = self._track.get('length', 0.0)
        if length <= 0:
            progress.update('')
            return
        frac = min(1.0, pos / length)
        bar_w = max(10, self.size.width - 16)
        filled = int(frac * bar_w)
        bar = '▓' * filled + '░' * (bar_w - filled)
        m1, s1 = divmod(int(pos), 60)
        m2, s2 = divmod(int(length), 60)
        progress.update(f'{m1}:{s1:02d} {bar} {m2}:{s2:02d}')

    def _tick_viz(self):
        viz = self._find('#viz', VizWidget)
        if viz is not None:
            viz.render_bars(self._viz.heights)

    async def on_unmount(self):
        await self._viz.stop()

    async def action_quit(self):
        self.exit()

    def _set_lyrics(self, lines: list[str], current: int = -1) -> None:
        widget = self._find('#lyrics', LyricsWidget)
        if widget is not None:
            widget.set_lyrics(lines, current)

    def _set_trivia(self, album: str, artist: str,
                    album_text: str, artist_text: str) -> None:
        """No-op when the panel is hidden with --no-trivia."""
        if not OPTS.trivia:
            return
        widget = self._find('#trivia', TriviaWidget)
        if widget is not None:
            widget.set_trivia(album, artist, album_text, artist_text)

    async def _control(self, *args: str) -> None:
        """Run a playerctl command against the player we're following, then
        re-poll so the UI reflects it without waiting for the next tick."""
        player = self._track.get('player', '')
        if not player:
            return
        await asyncio.to_thread(player_command, player, *args)
        await asyncio.sleep(0.15)   # let the player settle on D-Bus
        await self._poll()

    async def action_play_pause(self):
        await self._control('play-pause')

    async def action_next_track(self):
        await self._control('next')

    async def action_prev_track(self):
        await self._control('previous')

    async def action_seek_forward(self):
        await self._control('position', '5+')

    async def action_seek_back(self):
        await self._control('position', '5-')

    async def action_refresh(self):
        # A refresh should really refetch, so drop what we cached for this
        # track first — otherwise 'r' just replays the same wrong match.
        forget_track(self._track)
        self._track = {}
        await self._poll()


def _default_lang() -> str:
    """Derive the Wikipedia edition from the locale, e.g. pt_BR.UTF-8 -> pt."""
    for var in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = os.environ.get(var, '')
        if value and value not in ('C', 'POSIX'):
            code = value.split('.')[0].split('_')[0].lower()
            if len(code) == 2:
                return code
    return 'en'


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='legendary-lyrics',
        description='Terminal music visualizer with album art, synced lyrics '
                    'and bar animation.',
    )
    parser.add_argument(
        '-p', '--player', default='', metavar='NAME',
        help='follow a specific MPRIS player (e.g. spotify) instead of '
             'auto-selecting the one that is playing',
    )
    parser.add_argument(
        '-b', '--bars', type=int, default=60, metavar='N',
        help='number of visualizer bars (default: %(default)s)',
    )
    parser.add_argument(
        '--lang', default=_default_lang(), metavar='CODE',
        help='Wikipedia language edition for trivia, falling back to English '
             '(default: taken from your locale)',
    )
    parser.add_argument(
        '--no-trivia', action='store_true',
        help='hide the trivia panel and give the album art the whole column',
    )
    parser.add_argument(
        '--no-cache', action='store_true',
        help='never read or write the on-disk cache',
    )
    parser.add_argument(
        '--clear-cache', action='store_true',
        help='delete all cached lyrics, art and trivia, then exit',
    )
    parser.add_argument(
        '--list-players', action='store_true',
        help='list the MPRIS players currently registered, then exit',
    )
    parser.add_argument(
        '-V', '--version', action='version',
        version=f'legendary-lyrics {__version__}',
    )
    args = parser.parse_args()

    if args.clear_cache:
        print(f'Removed {clear_cache()} cached files from {_CACHE_DIR}')
        return 0

    OPTS.player = args.player
    OPTS.bars = max(8, min(256, args.bars))
    OPTS.trivia = not args.no_trivia
    OPTS.cache = not args.no_cache
    OPTS.lang = args.lang

    if args.list_players:
        players = _players_snapshot()
        if not players:
            print('No MPRIS players registered.')
            return 1
        for player in players:
            print(f"{player['player']:<24} {player['status']:<8} "
                  f"{player['artist']} — {player['title']}")
        return 0

    # Probe terminal capabilities before Textual takes over stdin: once the
    # app is running, its input loop competes with our raw tty reads for
    # escape-sequence responses, so queries done after start-up are unreliable.
    _kitty_supported()
    _get_cell_size()
    LegendaryLyricsApp().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
