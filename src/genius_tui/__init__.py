"""genius-tui — synced lyrics for whatever is playing, in your terminal."""

from .app import GeniusTui, Lyrics, Track, fetch_lyrics, get_now_playing, parse_lrc, run

__all__ = [
    "GeniusTui",
    "Lyrics",
    "Track",
    "fetch_lyrics",
    "get_now_playing",
    "parse_lrc",
    "run",
]
