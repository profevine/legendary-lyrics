#!/usr/bin/env python3
"""Legendary Lyrics — terminal music visualizer with album art, lyrics and bars."""

import asyncio
import base64
import io
import math
import os
import random
import re
import select as _select
import struct
import subprocess
import tempfile
import termios
import tty
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, Footer


# ─── playerctl ───────────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()


def get_track() -> dict:
    try:
        status = _run(['playerctl', 'status'])
        result: dict = {'status': status}
        for key, field in [
            ('artist', 'artist'), ('title', 'title'),
            ('album', 'album'), ('art_url', 'mpris:artUrl'),
        ]:
            try:
                result[key] = _run(['playerctl', 'metadata', field])
            except subprocess.CalledProcessError:
                result[key] = ''
        try:
            result['position'] = float(_run(['playerctl', 'position']))
        except (subprocess.CalledProcessError, ValueError):
            result['position'] = 0.0
        try:
            result['length'] = int(_run(['playerctl', 'metadata', 'mpris:length'])) / 1e6
        except (subprocess.CalledProcessError, ValueError):
            result['length'] = 0.0
        return result
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {'status': 'Stopped', 'title': '', 'artist': '', 'album': '',
                'art_url': '', 'position': 0.0, 'length': 0.0}


# ─── album art ───────────────────────────────────────────────────────────────

# Detect terminals that support the Kitty graphics protocol
_KITTY_SUPPORT = os.environ.get('TERM_PROGRAM', '') in ('kitty', 'ghostty') or \
                 os.environ.get('TERM', '') in ('xterm-kitty', 'xterm-ghostty')

_cell_size_cache: Optional[tuple[float, float]] = None


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


async def fetch_art_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        if url.startswith('file://'):
            return Path(url[7:]).read_bytes()
        async with httpx.AsyncClient() as c:
            r = await c.get(url, timeout=5.0, follow_redirects=True)
            r.raise_for_status()
            return r.content
    except Exception:
        return None


# ─── lyrics ──────────────────────────────────────────────────────────────────

def _parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    for line in text.splitlines():
        m = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)', line.strip())
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append((t, m.group(3).strip()))
    return sorted(out)


async def fetch_lyrics_data(artist: str, title: str, album: str = '') -> dict:
    if not (artist and title):
        return {}
    try:
        params: dict = {'artist_name': artist, 'track_name': title}
        if album:
            params['album_name'] = album
        async with httpx.AsyncClient() as c:
            r = await c.get('https://lrclib.net/api/get', params=params, timeout=10.0)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


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


# ─── widgets ─────────────────────────────────────────────────────────────────

_EIGHTH_BLOCKS = ' ▁▂▃▄▅▆▇█'


class VizWidget(Static):
    DEFAULT_CSS = """
    VizWidget {
        width: 30;
        height: 1fr;
        border: round $accent;
        padding: 0;
        overflow: hidden hidden;
    }
    """

    def render_bars(self, heights: list[float]):
        rows = self.content_size.height
        if rows <= 0:
            return
        text = Text(no_wrap=True)
        h_eighths = [int(h * rows * 8) for h in heights]

        for row in range(rows):
            rfb = rows - 1 - row
            level = (rfb + 1) / rows

            if level < 0.5:
                color = 'bright_green'
            elif level < 0.8:
                color = 'bright_yellow'
            else:
                color = 'bright_red'

            for he in h_eighths:
                e = he - rfb * 8
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
        width: 40%;
        height: 1fr;
        border: round $accent;
        padding: 0;
        overflow: hidden hidden;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._img_data: Optional[bytes] = None
        self._last_size: tuple[int, int] = (0, 0)
        self._resize_timer = None

    def set_image_data(self, data: Optional[bytes]):
        self._img_data = data
        self._last_size = (0, 0)
        if _KITTY_SUPPORT:
            self.update(Text(''))
            self.call_after_refresh(self._paint_kitty)
        else:
            self.call_later(self._paint_halfblock)

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
        self._last_size = (0, 0)   # force repaint at new dimensions
        if _KITTY_SUPPORT:
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
    #body { height: 1fr; }
    #progress {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    """
    BINDINGS = [
        ('q', 'quit', 'Quit'),
        ('r', 'refresh', 'Refresh'),
    ]

    def __init__(self):
        super().__init__()
        self._track: dict = {}
        self._lrc: list[tuple[float, str]] = []
        self._plain: list[str] = []
        self._viz = Visualizer(n_bars=26)

    def compose(self) -> ComposeResult:
        yield Static('Legendary Lyrics', id='info')
        with Horizontal(id='body'):
            yield ArtWidget(id='art')
            yield LyricsWidget(id='lyrics')
            yield VizWidget(id='viz')
        yield Static('', id='progress')
        yield Footer()

    async def on_mount(self):
        await self._viz.start()
        self.set_interval(1.0, self._poll)
        self.set_interval(0.5, self._sync_lrc)
        self.set_interval(1 / 20, self._tick_viz)
        await self._poll()

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

        self.query_one('#info', Static).update(f'{icon}  {title}{sep}{artist}')
        self._update_progress()

        if song_changed:
            self._lrc = []
            self._plain = []
            self.query_one('#lyrics', LyricsWidget).set_lyrics([])

            art_task = asyncio.create_task(fetch_art_bytes(track.get('art_url', '')))
            lyrics_task = asyncio.create_task(
                fetch_lyrics_data(
                    track.get('artist', ''), track.get('title', ''), track.get('album', '')
                )
            )
            art_data, lyrics_data = await asyncio.gather(art_task, lyrics_task)

            self.query_one('#art', ArtWidget).set_image_data(art_data)

            synced = lyrics_data.get('syncedLyrics', '')
            plain = lyrics_data.get('plainLyrics', '')

            if synced:
                self._lrc = _parse_lrc(synced)
                self._plain = []
                self._sync_lrc()
            elif plain:
                self._lrc = []
                self._plain = plain.splitlines()
                self.query_one('#lyrics', LyricsWidget).set_lyrics(self._plain)
            else:
                self.query_one('#lyrics', LyricsWidget).set_lyrics([])

    def _sync_lrc(self):
        if not self._lrc:
            return
        pos = self._track.get('position', 0.0)
        idx = 0
        for i, (t, _) in enumerate(self._lrc):
            if t <= pos:
                idx = i
        lines = [ln for _, ln in self._lrc]
        self.query_one('#lyrics', LyricsWidget).set_lyrics(lines, idx)

    def _update_progress(self):
        pos = self._track.get('position', 0.0)
        length = self._track.get('length', 0.0)
        if length <= 0:
            self.query_one('#progress', Static).update('')
            return
        frac = min(1.0, pos / length)
        bar_w = max(10, self.size.width - 16)
        filled = int(frac * bar_w)
        bar = '▓' * filled + '░' * (bar_w - filled)
        m1, s1 = divmod(int(pos), 60)
        m2, s2 = divmod(int(length), 60)
        self.query_one('#progress', Static).update(
            f'{m1}:{s1:02d} {bar} {m2}:{s2:02d}'
        )

    def _tick_viz(self):
        self.query_one('#viz', VizWidget).render_bars(self._viz.heights)

    async def on_unmount(self):
        await self._viz.stop()

    async def action_quit(self):
        self.exit()

    async def action_refresh(self):
        self._track = {}
        await self._poll()


if __name__ == '__main__':
    LegendaryLyricsApp().run()
