#!/usr/bin/env python3
"""Legendary Lyrics — terminal music visualizer with album art, lyrics and bars."""

import argparse
import asyncio
import base64
import colorsys
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
from bisect import bisect_right
from pathlib import Path
from typing import Optional

__version__ = '1.4.0'


# ─── options ─────────────────────────────────────────────────────────────────

class Options:
    """Runtime settings, populated from the command line in main()."""
    player: str = ''        # force a specific MPRIS player, '' = auto-select
    bars: int = 60          # visualizer bar count
    trivia: bool = True     # show the Wikipedia trivia panel
    cache: bool = True      # read/write the on-disk cache
    karaoke: bool = True    # animate synced lyrics instead of listing them
    lang: str = 'en'        # Wikipedia language edition
    mic: str = ''           # PipeWire source to monitor, '' = system default
    hear_me: bool = False   # start with the mic monitor on


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
from PIL import Image, ImageDraw, ImageFont
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option


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


# Transmitting with a=T creates a *new placement* every time, and placements
# persist until deleted — so repainting without deleting leaves the previous
# copy pinned where it was, on top of whatever now occupies those cells.
# Everything we draw carries this id so the delete can target only our image
# and leave alone anything other programs left in the terminal.
_KITTY_IMAGE_ID = 1971
_KITTY_DELETE = b'\x1b_Ga=d,d=I,i=%d\x1b\\' % _KITTY_IMAGE_ID


def _kitty_delete() -> None:
    """Remove our image and every placement of it."""
    try:
        with open('/dev/tty', 'wb') as tty_f:
            tty_f.write(_KITTY_DELETE)
    except Exception:
        pass


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
            hdr = (f'a=T,f=24,s={w},v={h},c={cols},r={rows},'
                   f'i={_KITTY_IMAGE_ID},m={more}')
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
        # What is actually driving the bars, shown in the panel's border so a
        # simulation is never mistaken for the real audio stream.
        self.source: str = 'starting…'
        # Whether a track is playing. The simulation has no idea on its own,
        # and bars dancing over a paused track look broken.
        self.active: bool = False
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
        except FileNotFoundError:
            self.source = 'simulated — cava not installed'
            await self._simulate()
            return
        except OSError:
            self.source = 'simulated — cava failed to start'
            await self._simulate()
            return

        try:
            frame_bytes = self.n_bars * 2
            while True:
                data = await self._proc.stdout.readexactly(frame_bytes)
                # Only a decoded frame proves cava is really feeding us; it can
                # start fine and then die on an input it cannot open.
                if self.source != 'cava':
                    self.source = 'cava'
                vals = struct.unpack(f'<{self.n_bars}H', data)
                self.heights = [v / 65535.0 for v in vals]
        except (asyncio.IncompleteReadError, OSError):
            self.source = 'simulated — cava stopped'
            await self._simulate()

    async def _simulate(self):
        phases = [random.uniform(0, math.tau) for _ in range(self.n_bars)]
        speeds = [random.uniform(0.08, 0.18) for _ in range(self.n_bars)]
        t = 0.0
        while True:
            if not self.active:
                # Decay to flat rather than cutting out, so pausing reads as
                # the music stopping instead of the app freezing.
                self.heights = [h * 0.85 for h in self.heights]
                await asyncio.sleep(1 / 30)
                continue
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


# ─── mic monitor ─────────────────────────────────────────────────────────────

def list_mics() -> list[tuple[str, str]]:
    """(name, description) of every capture source PipeWire knows, leaving out
    the '.monitor' sources, which are speakers, not microphones."""
    try:
        out = subprocess.check_output(
            ['pactl', '-f', 'json', 'list', 'sources'],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        sources = json.loads(out)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return []
    return [
        (s['name'], s.get('description') or s['name'])
        for s in sources
        if not s['name'].endswith('.monitor')
    ]


def default_mic() -> str:
    try:
        return subprocess.check_output(
            ['pactl', 'get-default-source'],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ''


class MicMonitor:
    """Plays a microphone straight into the default output, so you can hear
    yourself sing over the track on headphones.

    The audio never leaves PipeWire: pw-loopback links the source to the sink
    and asks for a 128-frame quantum, about 2.7 ms at 48 kHz, which is well
    under the ~10 ms where your own voice starts to sound like an echo. Asking
    for a smaller quantum gains nothing you can hear and starts to crackle.
    The stream shows up in the system mixer under its media name, so its
    volume is set there like any other app's."""

    QUANTUM = '128/48000'
    MEDIA_NAME = 'Legendary Lyrics mic monitor'

    def __init__(self, source: str = ''):
        self.source = source        # PipeWire source name; '' = system default
        self._proc: Optional[asyncio.subprocess.Process] = None

    @property
    def on(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> Optional[str]:
        """Start monitoring; returns why it could not, or None on success."""
        if self.on:
            return None
        cmd = [
            'pw-loopback', '-n', 'legendary-lyrics-mic', '-l', '3',
            '--capture-props',
            f'node.latency={self.QUANTUM} node.description="{self.MEDIA_NAME}"',
            '--playback-props',
            f'node.latency={self.QUANTUM} media.name="{self.MEDIA_NAME}" '
            f'node.description="{self.MEDIA_NAME}"',
        ]
        if self.source:
            cmd[1:1] = ['-C', self.source]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return 'pw-loopback not found (install pipewire)'
        except OSError as exc:
            return f'pw-loopback failed to start: {exc}'
        # A bad source name or no PipeWire makes it exit right away.
        try:
            await asyncio.wait_for(self._proc.wait(), 0.4)
        except asyncio.TimeoutError:
            return None
        self._proc = None
        return 'pw-loopback exited — is PipeWire running?'

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 2)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


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


# ─── karaoke ─────────────────────────────────────────────────────────────────

# Six stops sampled as a seamless loop, so a phase that only ever increases
# walks the whole spectrum and wraps without a seam.
_KARAOKE_PALETTE = (
    (255, 92, 138),     # rose
    (255, 168, 76),     # amber
    (250, 233, 120),    # butter
    (118, 238, 166),    # mint
    (108, 196, 255),    # sky
    (186, 148, 255),    # violet
)

# Off-stage lines are mixed towards this before being dimmed: the raw palette
# on its own goes muddy once it is darkened, and lyrics have to stay readable.
_QUIET_INK = (198, 205, 220)

# Colour cycles per second, and how much of the palette one line spans.
_PHASE_SPEED = 0.13
_PHASE_SPREAD = 0.85

# How long a line takes to materialise on stage, and the effects it can use.
_ENTRY_SECONDS = 0.34
_ENTRY_EFFECTS = ('expand', 'slide', 'decrypt', 'scatter')
_DECRYPT_GLYPHS = '#%&@$*+=<>/\\|01'


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _palette_rgb(t: float, palette: tuple = _KARAOKE_PALETTE) -> tuple[int, int, int]:
    n = len(palette)
    p = (t % 1.0) * n
    i = int(p)
    f = p - i
    a, b = palette[i], palette[(i + 1) % n]
    return (round(a[0] + (b[0] - a[0]) * f),
            round(a[1] + (b[1] - a[1]) * f),
            round(a[2] + (b[2] - a[2]) * f))


def _mix(a: tuple[int, int, int], b: tuple[int, int, int],
         t: float) -> tuple[int, int, int]:
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def _scale(color: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * k))) for c in color)


def _rgb(color: tuple[int, int, int], k: float = 1.0) -> str:
    """A Rich colour, dimmed or brightened by k. Everything on the karaoke
    stage is a true-colour value rather than a palette name, so a line can fade
    smoothly instead of stepping between 'dim' and 'bold'."""
    r, g, b = (max(0, min(255, round(c * k))) for c in color)
    return f'rgb({r},{g},{b})'


def _entry_alpha(effect: str, n: int, p: float, seed: int) -> list[float]:
    """Per-character opacity while a line materialises, in the spirit of the
    desktop screensaver's text effects."""
    if p >= 1.0 or n == 0:
        return [1.0] * n
    if effect == 'expand':
        centre = (n - 1) / 2
        radius = p * (centre + 2.0)
        return [_clamp01(radius - abs(i - centre) + 1.0) for i in range(n)]
    if effect == 'scatter':
        order = list(range(n))
        random.Random(seed).shuffle(order)
        rank = [0] * n
        for place, i in enumerate(order):
            rank[i] = place
        front = p * (n + 4)
        return [_clamp01(front - rank[i]) for i in range(n)]
    front = p * (n + 6) - 3      # 'slide' and 'decrypt' both sweep left to right
    return [_clamp01(front - i + 1.0) for i in range(n)]


def _entry_chars(effect: str, chars: list[str], alpha: list[float],
                 seed: int, frame: int) -> list[str]:
    """Scramble whatever the entry effect has not settled yet."""
    if effect != 'decrypt':
        return chars
    rnd = random.Random(seed * 7919 + frame)
    return [c if a >= 1.0 or not c.strip() else rnd.choice(_DECRYPT_GLYPHS)
            for c, a in zip(chars, alpha)]


def _wrap_line(text: str, width: int) -> list[str]:
    """Wrap on words so a long line stays readable when it is centred on the
    stage; Rich's own wrapping cannot be used here because every row is
    positioned by hand."""
    if width <= 0 or len(text) <= width:
        return [text]
    out: list[str] = []
    line = ''
    for word in text.split(' '):
        while len(word) > width:        # a single word wider than the panel
            if line:
                out.append(line)
                line = ''
            out.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += ' ' + word
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


# ─── big text ────────────────────────────────────────────────────────────────

# A terminal cannot resize its own font, so the track title is rasterised with
# PIL and painted as half-blocks — two roughly square pixels per cell, the same
# trick the album art falls back to. Going through a real font is what lets the
# banner carry accents, and anything else the system font covers, which a
# hand-drawn block alphabet never would.
_FONT_FALLBACKS = (
    '/usr/share/fonts/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/noto/NotoSans-Bold.ttf',
)

# Below this the letters turn to mush and plain terminal text reads better.
_MIN_TITLE_PX = 9

# Fewest cells a rasterised lyric line may occupy before it is not worth it.
_MIN_BIG_ROWS = 4

# A title is nearly always limited by the width rather than the height, so one
# size up is allowed to overflow this much and is then condensed back to fit —
# which uses the rows we have instead of dropping to a smaller, blurrier size.
_MAX_SQUEEZE = 0.8

# Type set to the very limit of its box crowds everything around it, so the
# size that fits is stepped back a fifth — never below what stays readable.
_TYPE_SCALE = 0.8

# Coverage is kept, not thresholded: a pixel only half covered by a stroke is
# drawn as a blend of the ink and whatever is behind it, which is what stops
# big type from breaking up into lego. These are the steps that blend is
# quantised to, and the curve that keeps the result from going soft.
_AA_LEVELS = 8
_AA_GAMMA = 0.7

# Columns that share one step of the gradient, and how many animation frames
# pass between banner repaints.
_HUE_STEP = 3
_BANNER_EVERY = 3

_UNPROBED = object()
_font_path = _UNPROBED
_big_cache: dict = {}
_BIG_CACHE_MAX = 24


def _info_rows(height: int) -> int:
    """How many rows the title bar gets: enough for a banner on a roomy
    terminal, and the single line it has always been when there is nothing to
    spare."""
    return max(3, min(8, height // 5))


def _title_font() -> Optional[str]:
    """A bold sans on disk. Asked of fontconfig first, since that is the only
    thing that knows what this system actually has installed."""
    global _font_path
    if _font_path is _UNPROBED:
        _font_path = None
        try:
            match = _run(['fc-match', '-f', '%{file}', 'sans-serif:bold'])
            if match and Path(match).is_file():
                _font_path = match
        except Exception:
            pass
        if _font_path is None:
            _font_path = next(
                (p for p in _FONT_FALLBACKS if Path(p).is_file()), None)
    return _font_path


def _fit_font(path: str, parts: tuple, cols: int, height: int):
    """The largest size at which every part fits the box, stepped back by
    _TYPE_SCALE so the type has room to breathe, and allowing for the
    condensing _draw_text does."""
    for size in range(height + 6, _MIN_TITLE_PX - 1, -1):
        font = ImageFont.truetype(path, size)
        for text in parts:
            left, top, right, bottom = font.getbbox(text)
            if not (0 < bottom - top <= height
                    and 0 < right - left <= cols / _MAX_SQUEEZE):
                break
        else:
            eased = max(_MIN_TITLE_PX, round(size * _TYPE_SCALE))
            return font if eased == size else ImageFont.truetype(path, eased)
    return None     # even at the smallest readable size it does not fit


def _ink_height(font, parts) -> int:
    """A canvas exactly as tall as the tallest part's letters, rounded up to a
    whole cell — anything more is dead space around the type."""
    ink = max(font.getbbox(text)[3] - font.getbbox(text)[1] for text in parts)
    return ink + ink % 2


# Coverage 0-255 to one of _AA_LEVELS steps, gamma-corrected once here so the
# paint loop only ever indexes a ramp.
_COVERAGE = [round((v / 255) ** _AA_GAMMA * _AA_LEVELS) for v in range(256)]


def _draw_text(font, text: str, cols: int, height: int) -> tuple:
    """Rasterise one line centred in a cols x height canvas of half-cell
    pixels. Returns the coverage grid and the columns its ink spans, which is
    what a karaoke sweep needs to know to cut the line at the right place."""
    left, top, right, bottom = font.getbbox(text)
    w, h = right - left, bottom - top
    img = Image.new('L', (w, h), 0)
    # getbbox measures the ink, not the em box, so offsetting by it centres on
    # the letters themselves rather than on the font's line spacing.
    ImageDraw.Draw(img).text((-left, -top), text, font=font, fill=255)
    if w > cols:
        img = img.resize((cols, h), Image.LANCZOS)
        w = cols
    px = img.load()

    grid = [[0] * cols for _ in range(height)]
    ox, oy = (cols - w) // 2, (height - h) // 2
    for y in range(h):
        row = grid[y + oy]
        for x in range(w):
            row[x + ox] = _COVERAGE[px[x, y]]
    return grid, ox, ox + w


def _cached(key, build):
    """Rasterising is far too slow to redo on every animation frame, and both
    the title and the line being sung change only every few seconds."""
    if key in _big_cache:
        return _big_cache[key]
    try:
        value = build()
    except Exception:
        value = None    # an unreadable font file is not worth crashing over
    if len(_big_cache) >= _BIG_CACHE_MAX:
        _big_cache.clear()
    _big_cache[key] = value
    return value


def _big_text(text: str, cols: int, rows: int) -> Optional[tuple]:
    """Rasterise text as large as fits in cols x rows cells, centred both ways.
    None when there is no font, or no room to draw it legibly."""
    def build():
        path = _title_font()
        if not (path and text.strip() and cols > 0 and rows > 0):
            return None
        font = _fit_font(path, (text,), cols, rows * 2)
        if font is None:
            return None
        return _draw_text(font, text, cols, _ink_height(font, (text,)))

    return _cached(('text', text, cols, rows), build)


def _split_evenly(text: str, parts: int) -> list:
    """Break a line into `parts` pieces of roughly equal length, on word
    boundaries. Empty when it cannot be split that many ways."""
    if parts == 1:
        return [text]
    words = text.split()
    if len(words) < parts:
        return []
    target = len(text) / parts
    out: list = []
    line = ''
    for word in words:
        if line and len(out) < parts - 1 and len(line) + 1 + len(word) > target:
            out.append(line)
            line = word
        else:
            line = f'{line} {word}' if line else word
    if line:
        out.append(line)
    return out if len(out) == parts else []


def _big_block(text: str, cols: int, rows: int) -> Optional[list]:
    """Rasterise a lyric line over as many rows as it takes. Wrapping is tried
    at one, two and three lines and the one that yields the largest type wins —
    a line too wide to draw at any size in one piece is usually drawn much
    bigger split in two. Every piece shares the winning size, so the line still
    reads as one piece of type."""
    def build():
        path = _title_font()
        if not (path and text.strip() and cols > 0 and rows > 0):
            return None

        best = None
        for parts in (1, 2, 3):
            per = rows // parts
            if per < _MIN_BIG_ROWS:
                break
            segments = _split_evenly(text, parts)
            if not segments:
                continue
            font = _fit_font(path, tuple(segments), cols, per * 2)
            if font and (best is None or font.size > best[0].size):
                best = (font, segments)
        if best is None:
            return None

        font, segments = best
        # One canvas height for every piece, so the gap between them stays even
        # whether or not a piece happens to carry a descender.
        height = _ink_height(font, segments)
        return segments, [_draw_text(font, s, cols, height) for s in segments]

    return _cached(('block', text, cols, rows), build)


def _panel_rgb(widget) -> tuple:
    """The colour actually behind a widget's content — what the half-covered
    edge of a letter has to fade into."""
    try:
        return widget.background_colors[1].rgb
    except Exception:
        return (12, 12, 16)


# Ramps are keyed on the ink rounded to a 32nd of each channel: the gradient
# drifts far too slowly for that to be visible, and it makes consecutive frames
# ask for the same ramp instead of building a new one 27 times a frame.
_RAMPS: dict = {}
_RAMP_CACHE_MAX = 4096


def _ramp(back: tuple, ink: tuple) -> tuple:
    """What a stroke of `ink` looks like at each coverage level once blended
    into the colour behind it, plus a memo of the composed cell styles. Painting
    a cell is then a dict lookup rather than two blends and a format."""
    key = (back, ink[0] & 0xF8, ink[1] & 0xF8, ink[2] & 0xF8)
    ramp = _RAMPS.get(key)
    if ramp is None:
        if len(_RAMPS) >= _RAMP_CACHE_MAX:
            _RAMPS.clear()
        ramp = _RAMPS[key] = ([_rgb(_mix(back, key[1:], i / _AA_LEVELS))
                               for i in range(_AA_LEVELS + 1)], {})
    return ramp


def _paint_row(top: list, bottom: list, width: int, ramps: list,
               alpha: Optional[list] = None) -> Text:
    """One row of a rasterised grid. Every inked cell is a ▀ whose foreground
    is the top half-pixel and whose background is the bottom one, so a cell can
    carry two different coverages; runs that share a style collapse into a
    single span."""
    line = Text(no_wrap=True)
    run, style, blanks = '', '', 0
    for x in range(width):
        ramp = ramps[x]
        up, down = top[x], bottom[x]
        if ramp is None or not (up or down):
            blanks += 1
            continue
        if alpha is not None:               # a line still materialising
            fade = alpha[x]
            up, down = round(up * fade), round(down * fade)
            if not (up or down):
                blanks += 1
                continue
        colours, memo = ramp
        here = memo.get((up, down))
        if here is None:
            here = memo[(up, down)] = f'{colours[up]} on {colours[down]}'
        if blanks or here != style:
            if run:
                line.append(run, style=style)
            line.append(' ' * blanks)
            run, style, blanks = '', here, 0
        run += '▀'
    if run:
        line.append(run, style=style)
    return line


def _hsv_rgb(hue: float, sat: float, val: float) -> tuple[int, int, int]:
    return tuple(round(c * 255)
                 for c in colorsys.hsv_to_rgb(hue % 1.0, min(1.0, max(0.0, sat)),
                                              min(1.0, max(0.0, val))))


# What a cover with no colour in it at all lends the title instead.
_SILVER = ((238, 242, 250), (166, 176, 196), (216, 222, 234), (134, 145, 166))


def _cover_palette(data: bytes) -> Optional[tuple]:
    """The vivid colours of an album cover, ordered around the colour wheel so
    a gradient running through them sweeps rather than lurches. Near-greys are
    dropped — they have nothing to lend, and a title painted in them would look
    broken rather than tasteful."""
    try:
        img = Image.open(io.BytesIO(data)).convert('RGB')
        img.thumbnail((80, 80))
        quantised = img.quantize(colors=24)
        table = quantised.getpalette() or []
        counts = quantised.getcolors() or []
    except Exception:
        return None     # not an image we can read; keep the built-in gradient

    picks: list = []
    for _, index in sorted(counts, reverse=True):
        r, g, b = table[index * 3:index * 3 + 3]
        hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if sat < 0.15 or val < 0.2:
            continue
        # A cover often quantises into a dozen shades of one colour; keeping
        # them all would make a "gradient" that never actually changes.
        if any(min(abs(hue - h), 1 - abs(hue - h)) < 0.05 for h, _, _ in picks):
            continue
        picks.append((hue, sat, val))
        if len(picks) == 6:
            break

    if not picks:
        return _SILVER
    if len(picks) < 3:
        # One colour on the cover still makes a gradient, just a tonal one.
        hue, sat, val = picks[0]
        sat, val = min(0.85, max(0.5, sat)), min(1.0, max(0.75, val))
        return tuple(_hsv_rgb(hue + d, sat * s, val * v) for d, s, v in
                     ((-0.05, 0.75, 1.0), (0.0, 1.0, 0.86),
                      (0.05, 0.8, 1.0), (0.0, 0.9, 0.78)))

    picks.sort()
    return tuple(_hsv_rgb(hue, min(0.85, max(0.5, sat)), min(1.0, max(0.75, val)))
                 for hue, sat, val in picks)


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

    def hide_kitty(self) -> None:
        """Erase the Kitty image. Its pixels live outside the cell grid, so
        anything drawn over this area — a modal, or the app's own widgets
        after a resize — would otherwise have the art showing through."""
        if not (_kitty_supported() and self._img_data):
            return
        _kitty_delete()
        self._last_size = (0, 0)   # force a repaint when it comes back

    def repaint(self) -> None:
        if _kitty_supported():
            self._paint_kitty()
        else:
            self._paint_halfblock()

    def on_resize(self) -> None:
        # Take the image down for the duration of the drag. The new layout is
        # drawn immediately while the repaint below is debounced, so leaving
        # it up would park the old art over freshly moved widgets.
        self.hide_kitty()
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
                tty_f.write(_KITTY_DELETE
                            + b'\x1b7' + cursor + kitty_seq + b'\x1b8')
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
    """Lyrics, either as a plain scrolling list or — when the track came with
    timestamps — as a karaoke stage: the line being sung sits in the middle of
    the panel, swept character by character in time with the music."""

    DEFAULT_CSS = """
    LyricsWidget {
        width: 1fr;
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """

    # A cue only says when a line starts, so the sweep runs at roughly singing
    # speed instead of being stretched across a trailing instrumental.
    _WIPE_MIN = 1.2
    _WIPE_PER_CHAR = 0.28

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lines: list[str] = []                  # unsynced lyrics
        self._entries: list[tuple[float, str]] = []  # synced lyrics
        self._times: list[float] = []
        self._pos = 0.0
        self._energy = 0.0
        self._stage_index = -1
        self._stage_since = 0.0
        self._frame = 0

    # ── what to show ────────────────────────────────────────────────────────

    def set_synced(self, entries: list[tuple[float, str]]) -> None:
        self._lines = []
        self._entries = entries
        self._times = [t for t, _ in entries]
        self._stage_index = -1
        self._draw()

    def set_plain(self, lines: list[str]) -> None:
        self._entries = []
        self._times = []
        self._lines = lines
        self._draw()

    def clear(self) -> None:
        self._lines = []
        self._entries = []
        self._times = []
        self._stage_index = -1
        self._draw()

    def tick(self, position: float, energy: float) -> None:
        """Called on every animation frame with the interpolated playback
        position and how loud the music is right now."""
        self._pos = position
        self._energy = energy
        self._frame += 1
        if not self._entries:
            return
        if OPTS.karaoke:
            self._draw()
        elif self._stage(position)[0] != self._stage_index:
            self._draw()

    def relayout(self) -> None:
        self._draw()

    def on_resize(self) -> None:
        self.call_later(self._draw)

    # ── drawing ─────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        if self._entries:
            stage, sung, wait = self._stage(self._pos)
            if OPTS.karaoke:
                width = max(8, self.content_size.width)
                height = max(3, self.content_size.height)
                self.update(self._render_stage(width, height, stage, sung, wait))
            else:
                self.update(self._render_list([s for _, s in self._entries], stage))
        elif self._lines:
            self.update(self._render_list(self._lines, -1))
        else:
            self.update(Text('Lyrics not found', style='dim italic'))

    def _stage(self, pos: float) -> tuple[int, float, Optional[float]]:
        """Which line is on stage, how much of it has been sung (0-1), and how
        long until the next one when we are waiting through an instrumental."""
        entries, times = self._entries, self._times
        i = bisect_right(times, pos) - 1

        nxt = None
        for j in range(max(0, i + 1), len(entries)):
            if times[j] > pos and entries[j][1].strip():
                nxt = j
                break

        # Before the first cue, or parked on one of the blank cues an LRC file
        # uses to mark an instrumental: put the upcoming line on stage early so
        # there is something to read before it has to be sung.
        if i < 0 or not entries[i][1].strip():
            if nxt is not None:
                return nxt, 0.0, times[nxt] - pos
            while i >= 0 and not entries[i][1].strip():
                i -= 1
            return i, 1.0, None

        start = times[i]
        span = times[i + 1] - start if i + 1 < len(times) else 6.0
        dur = max(self._WIPE_MIN,
                  min(span, self._WIPE_PER_CHAR * len(entries[i][1].strip()) + 0.8))
        sung = _clamp01((pos - start) / dur)

        wait = None
        if sung >= 1.0 and nxt is not None and times[nxt] - (start + dur) >= 3.0:
            wait = times[nxt] - pos
        return i, sung, wait

    def _render_stage(self, width: int, height: int, stage: int,
                      sung: float, wait: Optional[float]) -> Text:
        now = time.monotonic()
        phase = now * _PHASE_SPEED
        # Taller panels get more air between the lines, which is what makes the
        # sung line read as zoomed in rather than as one row among many.
        gap = sum(height >= tier for tier in (13, 26, 38))

        if stage != self._stage_index:
            self._stage_index = stage
            self._stage_since = now
        entering = _clamp01((now - self._stage_since) / _ENTRY_SECONDS)

        rows: list[Text] = []
        if stage >= 0:
            rows = self._active_rows(self._entries[stage][1].strip(), width,
                                     height, sung, entering, stage, phase)
        if wait is not None and wait <= 4.0:
            rows = ([self._pips(wait, width, phase)]
                    + ([Text()] if gap else []) + rows)

        above: list[Text] = []
        j, dist = stage - 1, 1
        while j >= 0 and len(above) < height:
            line = self._entries[j][1].strip()
            if line:
                above = (self._quiet_rows(line, width, dist, phase)
                         + [Text()] * gap + above)
                dist += 1
            j -= 1

        below: list[Text] = []
        j, dist = stage + 1, 1
        while j < len(self._entries) and len(below) < height:
            line = self._entries[j][1].strip()
            if line:
                below = (below + [Text()] * gap
                         + self._quiet_rows(line, width, dist, phase))
                dist += 1
            j += 1

        top = max(0, (height - len(rows)) // 2)
        above = above[-top:] if top else []
        out = [Text()] * (top - len(above)) + above + rows + below

        text = Text(no_wrap=True)
        for k, row in enumerate(out[:height]):
            if k:
                text.append('\n')
            text.append_text(row)
        return text

    def _active_rows(self, line: str, width: int, height: int, sung: float,
                     entering: float, seed: int, phase: float) -> list[Text]:
        """The line being sung, drawn as large as the panel allows. Falling
        back to ordinary terminal text keeps a narrow or short panel — or a
        machine with no font to rasterise from — working as it always did."""
        # Leave the stage half the panel at most, so the lines around it still
        # have somewhere to sit.
        budget = max(_MIN_BIG_ROWS, min(height - 4, height // 2))
        big = _big_block(line, width, budget)
        if big is None:
            return self._text_rows(line, width, sung, entering, seed, phase)
        return self._raster_rows(*big, width, sung, entering, seed, phase)

    def _raster_rows(self, segments: list, pieces: list, width: int,
                     sung: float, entering: float, seed: int,
                     phase: float) -> list[Text]:
        total = sum(len(s) for s in segments)
        glow = 1.0 + 0.22 * self._energy    # the sung text breathes with the music
        effect = _ENTRY_EFFECTS[seed % len(_ENTRY_EFFECTS)]
        back = _panel_rgb(self)
        pale = _ramp(back, (198, 206, 222))

        rows: list[Text] = []
        consumed = 0
        for segment, (grid, x0, x1) in zip(segments, pieces):
            span = max(1, x1 - x0)
            # The sweep runs over pixels rather than characters, so it can cut
            # a letter in half the way a karaoke wipe is supposed to.
            cut = x0 + span * _clamp01((sung * total - consumed) / len(segment))
            consumed += len(segment)
            drift = _HUE_STEP * _PHASE_SPREAD / span
            sungs = [_ramp(back, _scale(_palette_rgb(phase + i * drift), glow))
                     for i in range(span // _HUE_STEP + 1)]
            ramps: list = []
            for x in range(width):
                if not x0 <= x < x1:
                    ramps.append(None)
                else:
                    ramps.append(sungs[(x - x0) // _HUE_STEP]
                                 if x < cut else pale)

            # A line arriving on stage thins out rather than dimming: fading the
            # coverage keeps the anti-aliasing doing the work.
            fade = None
            if entering < 1.0:
                arriving = _entry_alpha(effect, span, entering, seed)
                fade = [arriving[x - x0] if x0 <= x < x1 else 0.0
                        for x in range(width)]

            if rows:
                rows.append(Text())         # air between the halves of a line
            for r in range(len(grid) // 2):
                rows.append(_paint_row(grid[r * 2], grid[r * 2 + 1], width,
                                       ramps, fade))
        return rows

    def _text_rows(self, line: str, width: int, sung: float, entering: float,
                   seed: int, phase: float) -> list[Text]:
        segments = _wrap_line(line, width)
        total = sum(len(s) for s in segments)
        if not total:
            return []

        # Zoom: a short line on a wide panel is letter-spaced so it fills the
        # stage instead of sitting small in the middle of it.
        step = min(2, width // total) if len(segments) == 1 else 1
        spread = step > 1

        effect = _ENTRY_EFFECTS[seed % len(_ENTRY_EFFECTS)]
        alpha = _entry_alpha(effect, total, entering, seed)
        chars = _entry_chars(effect, list(''.join(segments)), alpha,
                             seed, self._frame // 2)

        head = int(sung * total)
        glow = 1.0 + 0.22 * self._energy    # the sung text breathes with the music

        rows: list[Text] = []
        offset = 0
        for segment in segments:
            body = Text(no_wrap=True)
            for c in range(len(segment)):
                i = offset + c
                char, a = chars[i], alpha[i]
                if a <= 0.04 or not char.strip():
                    body.append(' ' * step)
                    continue
                if i < head:
                    hue = _palette_rgb(phase + i * _PHASE_SPREAD / total)
                    style = f'bold {_rgb(hue, glow * (0.45 + 0.55 * a))}'
                elif i == head:
                    style = f'bold {_rgb((255, 255, 255), 0.55 + 0.45 * a)}'
                else:
                    style = _rgb((190, 198, 215), 0.42 + 0.58 * a)
                body.append(char, style=style)
                if spread:
                    body.append(' ' * (step - 1))
            pad = max(0, (width - len(segment) * step) // 2)
            row = Text(' ' * pad, no_wrap=True)
            row.append_text(body)
            rows.append(row)
            offset += len(segment)
        return rows

    def _quiet_rows(self, line: str, width: int, dist: int,
                    phase: float) -> list[Text]:
        """A line that is not being sung: still tinted from the same gradient,
        but fading out the further it is from the stage."""
        style = _rgb(_mix(_palette_rgb(phase - 0.05 * dist), _QUIET_INK, 0.55),
                     max(0.40, 0.86 - 0.15 * dist))
        rows = []
        for segment in _wrap_line(line, width):
            pad = max(0, (width - len(segment)) // 2)
            rows.append(Text(' ' * pad + segment, style=style, no_wrap=True))
        return rows

    def _pips(self, wait: float, width: int, phase: float) -> Text:
        """The four-beat count-in before a line comes back after a gap."""
        lit = max(0, min(4, 4 - math.ceil(wait)))
        row = Text(' ' * max(0, (width - 7) // 2), no_wrap=True)
        for i in range(4):
            if i:
                row.append(' ')
            if i < lit:
                row.append('●', style=f'bold {_rgb(_palette_rgb(phase + i * 0.08))}')
            else:
                row.append('○', style=_rgb((120, 128, 148), 0.7))
        return row

    def _render_list(self, lines: list[str], current: int) -> Text:
        """The plain view: used for unsynced lyrics, and for synced ones when
        the animation is switched off."""
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
        return text


class InfoBar(Static):
    """The title bar. The track title is drawn as large as the terminal has
    room for, centred in both directions, with the artist and the player on a
    line of their own beneath it. Clicking anywhere on it opens the picker."""

    class Clicked(Message):
        pass

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._icon = ''
        self._title = 'Legendary Lyrics'
        self._artist = ''
        self._chip = Text()
        self._animated = False
        self._term_height = 0
        self._frame = 0
        self._palette = _KARAOKE_PALETTE

    def on_click(self) -> None:
        self.post_message(self.Clicked())

    def show(self, icon: str, title: str, artist: str, chip: Text) -> None:
        self._icon = icon
        self._title = title
        self._artist = artist
        self._chip = chip
        self._draw()

    def set_palette(self, palette: Optional[tuple]) -> None:
        """Paint the banner in the album cover's own colours; None goes back
        to the built-in gradient."""
        self._palette = palette or _KARAOKE_PALETTE
        self._draw()

    def tick(self) -> None:
        """Drift the banner's gradient — only while there is one to drift, and
        at a third of the frame rate, which a colour this slow cannot tell from
        the full one and which keeps a wide banner off the CPU."""
        self._frame += 1
        if self._animated and self._frame % _BANNER_EVERY == 0:
            self._draw()

    def relayout(self, terminal_height: int = 0) -> None:
        """The banner is sized from the whole terminal, not from this widget,
        so the app pushes the new height in — a Resize event carries it before
        app.size has caught up with it."""
        if terminal_height:
            self._term_height = terminal_height
        self._draw()

    def on_resize(self) -> None:
        self.call_later(self._draw)

    def _draw(self) -> None:
        width = self.content_size.width
        if not width:
            return      # no layout yet; the resize that follows will draw us

        rows = _info_rows(self._term_height or self.app.size.height)
        big = _big_text(self._title, width, rows - 1) if rows >= 4 else None
        self._animated = big is not None and OPTS.karaoke

        if big is None:
            line = Text(f'{self._icon}  {self._title}', no_wrap=True)
            if self._artist:
                line.append(f' — {self._artist}')
            line.append_text(self._chip)
            lines = [Text(), self._centre(line, width), Text()]
        else:
            phase = time.monotonic() * _PHASE_SPEED * 0.5 if self._animated else 0.0
            lines = self._banner(big[0], width, phase)
            sub = Text(f'{self._icon}  {self._artist}'.rstrip(), no_wrap=True)
            sub.append_text(self._chip)
            lines.append(self._centre(sub, width))

        text = Text(no_wrap=True)
        for i, line in enumerate(lines):
            if i:
                text.append('\n')
            text.append_text(line)
        self.update(text)

    def _banner(self, grid: list, width: int, phase: float) -> list:
        """Paint the rasterised title in the cover's colours."""
        back = _panel_rgb(self)
        spread = _HUE_STEP * 0.55 / width
        lines = []
        for r in range(len(grid) // 2):
            drift = phase + r * 0.04
            # One gradient step every few columns, so whole runs of cells share
            # a ramp instead of each carrying its own.
            groups = [_ramp(back, _palette_rgb(drift + i * spread, self._palette))
                      for i in range(width // _HUE_STEP + 1)]
            ramps = [groups[x // _HUE_STEP] for x in range(width)]
            lines.append(_paint_row(grid[r * 2], grid[r * 2 + 1], width, ramps))
        return lines

    @staticmethod
    def _centre(line: Text, width: int) -> Text:
        out = Text(' ' * max(0, (width - line.cell_len) // 2), no_wrap=True)
        out.append_text(line)
        return out


_AUTO = '@auto'


class PlayerPicker(ModalScreen[Optional[str]]):
    """Choose which MPRIS player to follow. Dismisses with the player's name,
    an empty string for automatic selection, or None when cancelled."""

    AUTO_FOCUS = '#picker-list'
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    DEFAULT_CSS = """
    PlayerPicker {
        align: center middle;
        background: $background 70%;
    }
    #picker {
        width: 80%;
        max-width: 76;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #picker-list {
        height: auto;
        max-height: 12;
        background: $panel;
        border: none;
        padding: 0;
    }
    #picker-hint {
        color: $foreground 50%;
        padding-top: 1;
    }
    """

    def __init__(self, players: list[dict], pinned: str):
        super().__init__()
        self._players = players
        self._pinned = pinned

    def _describe(self, player: dict) -> Text:
        icon = {'Playing': '▶', 'Paused': '⏸'}.get(player['status'], '⏹')
        marker = '● ' if player['player'] == self._pinned else '  '
        text = Text(f"{marker}{icon} {player['player']}", style='bold')
        title = player['title'] or '(nothing playing)'
        artist = f" — {player['artist']}" if player['artist'] else ''
        text.append(f'   {title}{artist}', style='dim')
        return text

    def compose(self) -> ComposeResult:
        options = [Option(
            Text('● ' if not self._pinned else '  ', style='bold').append(
                'Auto', style='bold').append(
                '   follow whichever player is playing', style='dim'),
            id=_AUTO,
        )]
        options += [Option(self._describe(p), id=p['player'])
                    for p in self._players]

        with Vertical(id='picker'):
            yield Static('Follow which player?', id='picker-title')
            yield OptionList(*options, id='picker-list')
            yield Static('enter select · esc cancel', id='picker-hint')

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id or _AUTO
        self.dismiss('' if chosen == _AUTO else chosen)

    def on_click(self, event: events.Click) -> None:
        # A click on the dimmed backdrop (rather than the panel) cancels.
        if event.widget is self:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ─── main app ────────────────────────────────────────────────────────────────

class LegendaryLyricsApp(App):
    TITLE = 'Legendary Lyrics'
    CSS = """
    Screen { background: $background; }

    #info {
        height: auto;
        padding: 0 1;
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
        ('s', 'choose_player', 'Player'),
        ('k', 'toggle_karaoke', 'Karaoke'),
        ('m', 'toggle_mic', 'Hear me'),
        ('M', 'next_mic', 'Mic'),
        ('r', 'refresh', 'Refresh'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self):
        super().__init__()
        self._track: dict = {}
        # When the position in _track was read. playerctl is only polled once a
        # second; the karaoke sweep and the progress bar interpolate from here
        # so they move smoothly between polls instead of jumping.
        self._pos_at: float = time.monotonic()
        self._viz = Visualizer(n_bars=OPTS.bars)
        self._mic = MicMonitor(OPTS.mic)

    def compose(self) -> ComposeResult:
        yield InfoBar(id='info')
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
        if OPTS.hear_me:
            await self.action_toggle_mic()
        self.set_interval(1.0, self._poll)
        self.set_interval(0.25, self._update_progress)
        self.set_interval(1 / 20, self._tick)
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
        self._pos_at = time.monotonic()

        song_changed = (
            track.get('title') != self._track.get('title') or
            track.get('artist') != self._track.get('artist')
        )
        self._track = track

        status = track.get('status', 'Stopped')
        icon = {'Playing': '▶', 'Paused': '⏸', 'Stopped': '⏹'}.get(status, '⏹')
        title = track.get('title') or 'Nothing playing'
        artist = track.get('artist') or ''

        self._viz.active = status == 'Playing'
        info = self._find('#info', InfoBar)
        if info is None:
            return
        chip = Text()
        player = track.get('player', '')
        if player:
            mode = 'pinned' if OPTS.player else 'auto'
            chip.append(f'   [{mode} · {player} ▾]', style='' if OPTS.player else 'dim')
        elif OPTS.player:
            # Explains an otherwise baffling "Nothing playing": we're pinned to
            # a player that isn't registered right now.
            chip.append(f'   [pinned · {OPTS.player} · not running ▾]', style='dim')
        else:
            chip.append('   [choose player ▾]', style='dim')
        info.show(icon, title, artist, chip)
        self._update_progress()

        if song_changed:
            self._clear_lyrics()
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
            # Quantising the cover is heavy enough to stutter the animation if
            # it runs on the event loop.
            palette = (await asyncio.to_thread(_cover_palette, art_data)
                       if art_data else None)
            info = self._find('#info', InfoBar)
            if info is not None:
                info.set_palette(palette)
            self._set_trivia(
                track.get('album', ''), track.get('artist', ''), album_text, artist_text
            )

            synced = lyrics_data.get('syncedLyrics', '')
            plain = lyrics_data.get('plainLyrics', '')

            lyrics = self._find('#lyrics', LyricsWidget)
            if lyrics is None:
                return
            if synced:
                lyrics.set_synced(_parse_lrc(synced))
            elif plain:
                lyrics.set_plain(plain.splitlines())
            else:
                lyrics.clear()

    def _position(self) -> float:
        """Where the track is right now, not where it was at the last poll."""
        pos = self._track.get('position', 0.0)
        if self._track.get('status') == 'Playing':
            pos += max(0.0, time.monotonic() - self._pos_at)
        length = self._track.get('length', 0.0)
        return min(pos, length) if length > 0 else pos

    def _update_progress(self):
        progress = self._find('#progress', Static)
        if progress is None:
            return
        pos = self._position()
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

    def _tick(self):
        heights = self._viz.heights
        viz = self._find('#viz', VizWidget)
        if viz is not None:
            if viz.border_title != self._viz.source:
                viz.border_title = self._viz.source
            viz.render_bars(heights)
        lyrics = self._find('#lyrics', LyricsWidget)
        if lyrics is not None:
            energy = max(heights) if heights else 0.0
            lyrics.tick(self._position(), energy)
        info = self._find('#info', InfoBar)
        if info is not None:
            info.tick()

    async def on_unmount(self):
        await self._mic.stop()
        await self._viz.stop()
        if _kitty_supported():
            # Otherwise the album art stays on the terminal after we exit.
            _kitty_delete()

    async def action_quit(self):
        self.exit()

    def _clear_lyrics(self) -> None:
        widget = self._find('#lyrics', LyricsWidget)
        if widget is not None:
            widget.clear()

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

    async def on_info_bar_clicked(self, _: InfoBar.Clicked) -> None:
        await self.action_choose_player()

    async def action_choose_player(self) -> None:
        players = await asyncio.to_thread(_players_snapshot)
        art = self._find('#art', ArtWidget)
        if art is not None:
            art.hide_kitty()
        self.push_screen(PlayerPicker(players, OPTS.player), self._player_chosen)

    def _player_chosen(self, choice: Optional[str]) -> None:
        art = self._find('#art', ArtWidget)
        if art is not None:
            art.repaint()
        if choice is None:            # cancelled
            return
        OPTS.player = choice          # '' restores automatic selection
        self._track = {}              # force a full refetch for the new player
        self.call_later(self._poll)

    def on_resize(self, event: events.Resize) -> None:
        # How tall the title banner may be comes from the whole terminal, which
        # the title bar cannot see a change in on its own.
        info = self._find('#info', InfoBar)
        if info is not None:
            info.relayout(event.size.height)

    def action_toggle_karaoke(self) -> None:
        OPTS.karaoke = not OPTS.karaoke
        for selector, kind in (('#lyrics', LyricsWidget), ('#info', InfoBar)):
            widget = self._find(selector, kind)
            if widget is not None:
                widget.relayout()

    def _mic_label(self) -> str:
        name = self._mic.source or default_mic()
        for source, description in list_mics():
            if source == name:
                return description
        return name or 'default microphone'

    async def action_toggle_mic(self) -> None:
        if self._mic.on:
            await self._mic.stop()
            self.notify('Mic monitor off')
            return
        error = await self._mic.start()
        if error:
            self.notify(error, title='Mic monitor', severity='error')
            return
        label = await asyncio.to_thread(self._mic_label)
        self.notify(f'Hearing {label} — use headphones, or the speakers '
                    'will feed back', title='Mic monitor on')

    async def action_next_mic(self) -> None:
        mics = await asyncio.to_thread(list_mics)
        if not mics:
            self.notify('No microphones found', severity='warning')
            return
        names = [name for name, _ in mics]
        current = self._mic.source or await asyncio.to_thread(default_mic)
        index = names.index(current) + 1 if current in names else 0
        name, description = mics[index % len(mics)]
        self._mic.source = name
        if self._mic.on:
            # Relink to the new source; the gap is a fraction of a second.
            await self._mic.stop()
            error = await self._mic.start()
            if error:
                self.notify(error, title='Mic monitor', severity='error')
                return
        self.notify(description, title='Mic' + (' (monitoring)'
                                                 if self._mic.on else ''))

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
        '--no-karaoke', action='store_true',
        help='show synced lyrics as a plain list instead of the animated '
             'sing-along stage',
    )
    parser.add_argument(
        '--hear-me', action='store_true',
        help='start with the mic monitor on, playing your microphone into '
             'your headphones so you hear yourself sing (toggle with m)',
    )
    parser.add_argument(
        '--mic', default='', metavar='SOURCE',
        help='PipeWire source to monitor instead of the default microphone '
             '(see --list-mics; cycle in the app with M)',
    )
    parser.add_argument(
        '--list-mics', action='store_true',
        help='list the microphones the mic monitor can use, then exit',
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
    OPTS.karaoke = not args.no_karaoke
    OPTS.lang = args.lang
    OPTS.mic = args.mic
    OPTS.hear_me = args.hear_me

    if args.list_mics:
        mics = list_mics()
        if not mics:
            print('No microphones found (is PipeWire running?).')
            return 1
        default = default_mic()
        for name, description in mics:
            mark = '*' if name == default else ' '
            print(f'{mark} {name}\n    {description}')
        return 0

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
    _title_font()
    LegendaryLyricsApp().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
