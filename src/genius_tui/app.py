"""genius-tui — synced lyrics for whatever is playing, in your terminal.

Detects the currently playing song system-wide on macOS, fetches
time-synchronized lyrics from LRCLIB (free, no API key), and falls back to
scraping plain lyrics from Genius when no synced version exists.

Run:  genius-tui  (or: uvx genius-tui)
Keys: q quit · r refresh · +/- sync offset · f toggle follow · h footer · l lyrics only · o open in Genius
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html as htmllib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import time
import webbrowser
from bisect import bisect_right
from dataclasses import dataclass, field

import httpx
from PIL import Image
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Footer, Static
from textual_image.widget import TGPImage

USER_AGENT = "genius-tui/0.1 (https://github.com/samforeman/genius-tui)"
POLL_SECONDS = 1.0
TICK_SECONDS = 0.25
SCROLLBAR_VISIBLE_SECONDS = 1.0
FOLLOW_SCROLLBAR_THRESHOLD = 6


def terminal_prefers_light_theme() -> bool:
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        try:
            return int(colorfgbg.rsplit(";", 1)[-1]) >= 7
        except ValueError:
            pass
    appearance = os.environ.get("APPLE_INTERFACE_STYLE", "")
    if appearance:
        return appearance.lower() != "dark"
    return platform.system() == "Darwin"


# --------------------------------------------------------------------------
# Now-playing detection (macOS, system-wide with per-app fallbacks)
# --------------------------------------------------------------------------


@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    artwork_data: str = ""
    duration: float = 0.0  # seconds
    position: float = 0.0  # seconds, at time `grabbed`
    playing: bool = True
    grabbed: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> tuple[str, str]:
        return (self.title.lower().strip(), self.artist.lower().strip())

    def position_now(self, offset: float = 0.0) -> float:
        pos = self.position
        if self.playing:
            pos += time.monotonic() - self.grabbed
        return max(0.0, pos + offset)


def _run(cmd: list[str], timeout: float = 4.0) -> str | None:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _from_media_control() -> Track | None:
    """ungive/media-control — works system-wide incl. macOS 15.4+."""
    if not shutil.which("media-control"):
        return None
    raw = _run(["media-control", "get"])
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not data or not data.get("title"):
        return None
    pos = float(data.get("elapsedTime") or 0.0)
    playing = bool(data.get("playing"))
    # elapsedTime is a snapshot taken at `timestamp`; extrapolate if playing.
    ts = data.get("timestamp")
    if playing and ts:
        try:
            from datetime import datetime, timezone

            then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            pos += (datetime.now(timezone.utc) - then).total_seconds()
        except ValueError:
            pass
    return Track(
        title=str(data.get("title") or ""),
        artist=str(data.get("artist") or ""),
        album=str(data.get("album") or ""),
        artwork_data=str(data.get("artworkData") or ""),
        duration=float(data.get("duration") or 0.0),
        position=pos,
        playing=playing,
    )


def _from_nowplaying_cli() -> Track | None:
    """kirtan-shah/nowplaying-cli — pre-15.4 macOS."""
    if not shutil.which("nowplaying-cli"):
        return None
    raw = _run(
        ["nowplaying-cli", "get", "title", "artist", "album", "duration",
         "elapsedTime", "playbackRate"]
    )
    if not raw:
        return None
    vals = raw.splitlines()
    if len(vals) < 6 or vals[0] in ("null", ""):
        return None

    def num(s: str) -> float:
        try:
            return float(s)
        except ValueError:
            return 0.0

    return Track(
        title=vals[0],
        artist=vals[1] if vals[1] != "null" else "",
        album=vals[2] if vals[2] != "null" else "",
        duration=num(vals[3]),
        position=num(vals[4]),
        playing=num(vals[5]) > 0,
    )


_OSA_SEP = "|~|"


def _from_applescript(app: str, ms_duration: bool) -> Track | None:
    script = f'''
    if application "{app}" is running then
        tell application "{app}"
            if player state is playing or player state is paused then
                set t to current track
                return (name of t) & "{_OSA_SEP}" & (artist of t) & \
"{_OSA_SEP}" & (album of t) & "{_OSA_SEP}" & (duration of t) & \
"{_OSA_SEP}" & (player position) & "{_OSA_SEP}" & (player state as text)
            end if
        end tell
    end if
    '''
    raw = _run(["osascript", "-e", script])
    if not raw or _OSA_SEP not in raw:
        return None
    parts = raw.split(_OSA_SEP)
    if len(parts) != 6:
        return None
    title, artist, album, dur, pos, state = parts
    try:
        duration = float(dur.replace(",", "."))
        position = float(pos.replace(",", "."))
    except ValueError:
        return None
    if ms_duration:
        duration /= 1000.0
    return Track(
        title=title, artist=artist, album=album,
        duration=duration, position=position,
        playing=state.strip().lower() == "playing",
    )


_BACKENDS = [
    ("media-control", _from_media_control),
    ("nowplaying-cli", _from_nowplaying_cli),
    ("Spotify", lambda: _from_applescript("Spotify", ms_duration=True)),
    ("Music", lambda: _from_applescript("Music", ms_duration=False)),
]
_last_backend: int | None = None


def get_now_playing() -> tuple[Track | None, str]:
    """Try the last-working backend first, then the rest in order."""
    global _last_backend
    order = list(range(len(_BACKENDS)))
    if _last_backend is not None:
        order.remove(_last_backend)
        order.insert(0, _last_backend)
    for i in order:
        name, fn = _BACKENDS[i]
        track = fn()
        if track and track.title:
            _last_backend = i
            return track, name
    _last_backend = None
    return None, ""


# --------------------------------------------------------------------------
# Lyrics: LRC parsing, LRCLIB, Genius fallback
# --------------------------------------------------------------------------

_LRC_TS = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse LRC text into a sorted list of (seconds, line)."""
    entries: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = _LRC_TS.findall(raw)
        if not stamps:
            continue
        content = _LRC_TS.sub("", raw).strip()
        for minutes, rest in stamps:
            secs = float(rest.replace(":", "."))
            entries.append((int(minutes) * 60 + secs, content))
    entries.sort(key=lambda e: e[0])
    return entries


@dataclass
class Lyrics:
    source: str  # "LRCLIB (synced)" | "LRCLIB (plain)" | "Genius (plain)"
    synced: list[tuple[float, str]] | None = None
    plain: list[str] | None = None
    url: str = ""


async def fetch_lrclib(client: httpx.AsyncClient, track: Track) -> Lyrics | None:
    params = {"artist_name": track.artist, "track_name": track.title}
    if track.album:
        params["album_name"] = track.album
    if track.duration:
        params["duration"] = str(round(track.duration))
    hit = None
    try:
        r = await client.get("https://lrclib.net/api/get", params=params)
        if r.status_code == 200:
            hit = r.json()
    except httpx.HTTPError:
        return None
    if hit is None:  # exact match failed -> search
        try:
            r = await client.get(
                "https://lrclib.net/api/search",
                params={"track_name": track.title, "artist_name": track.artist},
            )
            if r.status_code == 200:
                for cand in r.json():
                    if (
                        not track.duration
                        or abs((cand.get("duration") or 0) - track.duration) <= 5
                    ):
                        hit = cand
                        break
        except httpx.HTTPError:
            return None
    if not hit:
        return None
    url = f"https://lrclib.net/api/get/{hit.get('id', '')}"
    if hit.get("syncedLyrics"):
        return Lyrics("LRCLIB (synced)", synced=parse_lrc(hit["syncedLyrics"]), url=url)
    if hit.get("plainLyrics"):
        return Lyrics("LRCLIB (plain)", plain=hit["plainLyrics"].splitlines(), url=url)
    return None


def _extract_lyrics_containers(page: str) -> str:
    """Pull text out of Genius's `data-lyrics-container` divs (depth-aware)."""
    chunks: list[str] = []
    for m in re.finditer(r'<div[^>]*data-lyrics-container="true"[^>]*>', page):
        depth, start, i = 1, m.end(), m.end()
        for tag in re.finditer(r"<(/?)div\b", page[m.end():]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                i = m.end() + tag.start()
                break
        chunks.append(page[start:i])
    text = "\n".join(chunks)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?div[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = htmllib.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    # collapse runs of blank lines
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


async def fetch_genius_url(client: httpx.AsyncClient, track: Track) -> str | None:
    """Find the Genius song page URL for a track."""
    q = f"{track.artist} {track.title}".strip()
    try:
        r = await client.get(
            "https://genius.com/api/search/multi", params={"q": q}
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    for section in data.get("response", {}).get("sections", []):
        for hit in section.get("hits", []):
            result = hit.get("result", {})
            if hit.get("type") == "song" and result.get("url"):
                return result["url"]
    return None


async def fetch_genius(client: httpx.AsyncClient, track: Track) -> Lyrics | None:
    url = await fetch_genius_url(client, track)
    if not url:
        return None
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    text = _extract_lyrics_containers(r.text)
    if not text:
        return None
    return Lyrics("Genius (plain)", plain=text.splitlines(), url=url)


async def fetch_lyrics(client: httpx.AsyncClient, track: Track) -> Lyrics | None:
    return await fetch_lrclib(client, track) or await fetch_genius(client, track)


# --------------------------------------------------------------------------
# TUI
# --------------------------------------------------------------------------


def fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def decode_album_art_image(artwork_data: str) -> Image.Image | None:
    if not artwork_data:
        return None
    try:
        raw = base64.b64decode(artwork_data, validate=True)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (binascii.Error, OSError, ValueError):
        return None


async def fetch_image_url(client: httpx.AsyncClient, url: str) -> Image.Image | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except (httpx.HTTPError, OSError):
        return None


async def fetch_album_art(client: httpx.AsyncClient, track: Track) -> Image.Image | None:
    try:
        genius = await client.get(
            "https://genius.com/api/search/multi",
            params={"q": f"{track.artist} {track.title}"},
        )
        genius.raise_for_status()
        for section in genius.json().get("response", {}).get("sections", []):
            for hit in section.get("hits", []):
                result = hit.get("result", {})
                if hit.get("type") == "song":
                    url = result.get("song_art_image_url") or result.get("header_image_url")
                    if url and (image := await fetch_image_url(client, url)):
                        return image
    except (httpx.HTTPError, json.JSONDecodeError):
        pass
    try:
        response = await client.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{track.artist} {track.title}",
                "entity": "song",
                "limit": "1",
            },
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results and (url := results[0].get("artworkUrl100")):
            return await fetch_image_url(client, url)
    except (httpx.HTTPError, json.JSONDecodeError):
        pass
    return None


class StableTGPImage(TGPImage, Renderable=TGPImage._Renderable):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._render_key = None

    def render(self):
        if not self.image:
            return ""
        size = self._get_styled_size()
        key = (size, self.region)
        if not self._renderable or self._render_key != key:
            if self._renderable:
                self._renderable.cleanup()
            self._render_key = key
            self._renderable = self._Renderable(self.image, *size)
        return self._renderable


class LyricLine(Static):
    def __init__(self, line: str, *args: object, **kwargs: object) -> None:
        super().__init__(f"  {line}", *args, **kwargs)
        self.line = line

    def set_current(self, current: bool) -> None:
        self.update(f"▸ {self.line}" if current else f"  {self.line}")


class GeniusTui(App):
    TITLE = "genius-tui"

    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; padding: 0 0 0 1; }
    #track-info { width: 1fr; height: auto; padding: 0 0 0 1; }
    #title { color: $text; text-style: bold; }
    #artist-album { color: $text-muted; }
    #source { color: $text-muted; }
    #time { color: $text-muted; }
    #album-art { width: 13; height: 4; }
    #lyrics { height: 1fr; padding: 1 4; overflow-y: scroll; scrollbar-size-vertical: 0; }
    #lyrics.scrollbar-visible { scrollbar-size-vertical: 2; }
    #lyrics.lyrics-only { scrollbar-size-vertical: 0; }
    LyricLine { width: 100%; text-align: left; color: $text-muted; }
    LyricLine.past { color: $text-muted; text-style: dim; }
    LyricLine.current { color: ansi_blue; text-style: bold; }
    .message { width: 100%; content-align: center middle; color: $text-muted; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("plus,equals_sign", "offset(0.5)", "Delay +0.5s"),
        ("minus", "offset(-0.5)", "Delay -0.5s"),
        ("f", "toggle_follow", "Follow"),
        ("h", "toggle_footer", "Footer"),
        ("l", "toggle_lyrics_only", "Lyrics only"),
        ("o", "open_genius", "Open in Genius"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.theme = "ansi-light" if terminal_prefers_light_theme() else "ansi-dark"
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=10
        )
        self.track: Track | None = None
        self.lyrics: Lyrics | None = None
        self.backend = ""
        self.offset = 0.0
        self.follow = True
        self.footer_hidden = False
        self.lyrics_only = False
        self.current_idx = -1
        self.genius_url: str | None = None
        self._artwork_key: str = ""
        self._artwork_track_key: tuple[str, str] | None = None
        self._metadata: dict[str, str] = {}
        self._artwork_task: asyncio.Task | None = None
        self._genius_task: asyncio.Task | None = None
        self._fetch_task: asyncio.Task | None = None
        self._scrollbar_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Horizontal(
            StableTGPImage(id="album-art"),
            Vertical(
                Static("  0:00 / 0:00", id="time"),
                Static("  waiting for music…", id="title"),
                Static("  ", id="artist-album"),
                Static("  —    —", id="source"),
                id="track-info",
            ),
            id="top",
        )
        yield VerticalScroll(
            Static("Nothing playing yet.", classes="message"), id="lyrics"
        )
        yield Footer(id="footer")

    def on_mount(self) -> None:
        self.update_album_art(None)
        self.hide_scrollbar()
        self.set_interval(POLL_SECONDS, self.poll_player)
        self.set_interval(TICK_SECONDS, self.tick)
        self.call_later(self.poll_player)

    async def on_unmount(self) -> None:
        if self._artwork_task:
            self._artwork_task.cancel()
        if self._genius_task:
            self._genius_task.cancel()
        await self.client.aclose()

    def update_static(self, selector: str, value: str) -> None:
        if self._metadata.get(selector) != value:
            self._metadata[selector] = value
            self.query_one(selector, Static).update(value)

    # -- player polling ----------------------------------------------------

    async def poll_player(self) -> None:
        track, backend = await asyncio.to_thread(get_now_playing)
        self.backend = backend
        old_key = self.track.key if self.track else None
        self.track = track
        if track is None:
            self.update_static("#time", "  0:00 / 0:00")
            self.update_static("#title", "  nothing playing")
            self.update_static("#artist-album", "  ")
            self.update_static("#source", "  —    —")
            self.update_album_art(None)
            return
        self.update_static("#title", f"  {track.title}")
        album = f"  󰀥  {track.album}" if track.album else ""
        self.update_static("#artist-album", f"  {track.artist}{album}")
        self.update_album_art(track)
        if track.key != old_key:
            self.lyrics = None
            self.current_idx = -1
            self.genius_url = None
            self.show_message("Fetching lyrics…")
            if self._fetch_task:
                self._fetch_task.cancel()
            self._fetch_task = asyncio.create_task(self.load_lyrics(track))
            if self._genius_task:
                self._genius_task.cancel()
            self._genius_task = asyncio.create_task(self.load_genius_url(track))

    async def load_lyrics(self, track: Track) -> None:
        try:
            lyrics = await fetch_lyrics(self.client, track)
        except asyncio.CancelledError:
            return
        if self.track and self.track.key == track.key:
            self.lyrics = lyrics
            if lyrics is None:
                self.show_message("No lyrics found for this track.")
            else:
                await self.show_lyrics(lyrics)

    async def load_genius_url(self, track: Track) -> None:
        try:
            url = await fetch_genius_url(self.client, track)
        except asyncio.CancelledError:
            return
        finally:
            if self._genius_task is asyncio.current_task():
                self._genius_task = None
        if self.track and self.track.key == track.key:
            self.genius_url = url

    def apply_layout(self) -> None:
        show_chrome = not self.lyrics_only
        art = self.query_one("#album-art", StableTGPImage)
        self.query_one("#top", Horizontal).display = show_chrome
        self.query_one("#footer", Footer).display = show_chrome and not self.footer_hidden
        art.display = show_chrome and art.image is not None

    def update_album_art(self, track: Track | None) -> None:
        art = self.query_one("#album-art", StableTGPImage)
        key = track.artwork_data if track else ""
        track_key = track.key if track else None
        if track is None:
            if self._artwork_task:
                self._artwork_task.cancel()
                self._artwork_task = None
            self._artwork_key = ""
            self._artwork_track_key = None
            art.image = None
            self.apply_layout()
            return
        if key:
            if self._artwork_task:
                self._artwork_task.cancel()
                self._artwork_task = None
            if key != self._artwork_key:
                self._artwork_key = key
                self._artwork_track_key = track_key
                art.image = decode_album_art_image(key)
            self.apply_layout()
            return
        if self._artwork_track_key == track_key:
            self.apply_layout()
            return
        if self._artwork_task:
            self._artwork_task.cancel()
            self._artwork_task = None
        self._artwork_key = ""
        self._artwork_track_key = track_key
        art.image = None
        self.apply_layout()
        self._artwork_task = asyncio.create_task(self.load_album_art(track))

    async def load_album_art(self, track: Track) -> None:
        task = asyncio.current_task()
        try:
            image = await fetch_album_art(self.client, track)
        except asyncio.CancelledError:
            return
        finally:
            if self._artwork_task is task:
                self._artwork_task = None
        if self.track and self.track.key == track.key and image is not None:
            art = self.query_one("#album-art", StableTGPImage)
            art.image = image
            self.apply_layout()

    # -- rendering ---------------------------------------------------------

    def show_message(self, text: str) -> None:
        box = self.query_one("#lyrics", VerticalScroll)
        box.remove_children()
        box.mount(Static(text, classes="message"))

    async def show_lyrics(self, lyrics: Lyrics) -> None:
        box = self.query_one("#lyrics", VerticalScroll)
        await box.remove_children()
        lines = (
            [ln for _, ln in lyrics.synced]
            if lyrics.synced
            else (lyrics.plain or [])
        )
        await box.mount_all(
            LyricLine(line or " ", classes="line") for line in lines
        )
        box.scroll_home(animate=False)
        self.current_idx = -1

    def tick(self) -> None:
        if self.track:
            pos = self.track.position_now()
            self.update_static(
                "#time", f"  {fmt_time(pos)} / {fmt_time(self.track.duration)}"
            )
        source = self.lyrics.source if self.lyrics else "—"
        backend = f"via {self.backend}" if self.backend else "—"
        self.update_static("#source", f"  {source}    {backend}")

        if not (self.track and self.lyrics):
            return
        lines = list(self.query(LyricLine))
        if not lines:
            return
        pos = self.track.position_now(self.offset)
        if self.lyrics.synced:
            times = [t for t, _ in self.lyrics.synced]
            idx = max(0, bisect_right(times, pos) - 1)
            if pos < times[0]:
                idx = -1
        elif self.track.duration:
            idx = min(
                len(lines) - 1, int(pos / self.track.duration * len(lines))
            )
        else:
            return
        if idx == self.current_idx:
            return
        jumped = (
            self.current_idx >= 0
            and abs(idx - self.current_idx) >= FOLLOW_SCROLLBAR_THRESHOLD
        )
        self.current_idx = idx
        for i, w in enumerate(lines):
            current = i == idx
            w.set_current(current)
            w.set_class(current, "current")
            w.set_class(i < idx, "past")
        if self.follow and 0 <= idx < len(lines):
            box = self.query_one("#lyrics", VerticalScroll)
            box.scroll_to_center(lines[idx], animate=False)
            if jumped:
                self.show_scrollbar_temporarily()

    def show_scrollbar_temporarily(self) -> None:
        lyrics = self.query_one("#lyrics", VerticalScroll)
        if self.lyrics_only:
            self.hide_scrollbar()
            return
        lyrics.show_vertical_scrollbar = True
        lyrics.set_class(True, "scrollbar-visible")
        if self._scrollbar_timer:
            self._scrollbar_timer.stop()
        self._scrollbar_timer = self.set_timer(
            SCROLLBAR_VISIBLE_SECONDS, self.hide_scrollbar
        )

    def hide_scrollbar(self) -> None:
        lyrics = self.query_one("#lyrics", VerticalScroll)
        lyrics.show_vertical_scrollbar = False
        lyrics.set_class(False, "scrollbar-visible")

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.show_scrollbar_temporarily()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.show_scrollbar_temporarily()

    def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "down", "pageup", "pagedown", "home", "end"}:
            self.show_scrollbar_temporarily()

    # -- actions -------------------------------------------------------------

    def action_refresh(self) -> None:
        if self.track:
            track = self.track
            self.lyrics = None
            self.show_message("Refetching lyrics…")
            if self._fetch_task:
                self._fetch_task.cancel()
            self._fetch_task = asyncio.create_task(self.load_lyrics(track))

    def action_offset(self, delta: float) -> None:
        self.offset = round(self.offset + delta, 1)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow

    def action_toggle_footer(self) -> None:
        self.footer_hidden = not self.footer_hidden
        self.apply_layout()

    def action_toggle_lyrics_only(self) -> None:
        self.lyrics_only = not self.lyrics_only
        lyrics = self.query_one("#lyrics", VerticalScroll)
        lyrics.set_class(self.lyrics_only, "lyrics-only")
        self.hide_scrollbar()
        self.apply_layout()

    def action_open_genius(self) -> None:
        if self.genius_url:
            webbrowser.open(self.genius_url)
            self.notify(f"Opened {self.genius_url}")
        elif self.track:
            self.notify("No Genius page found yet for this track.", severity="warning")
        else:
            self.notify("Nothing playing.", severity="warning")


def run() -> None:
    """Console-script entry point."""
    GeniusTui().run()


if __name__ == "__main__":
    run()
