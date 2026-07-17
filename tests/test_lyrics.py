import time

import pytest

from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from genius_tui.app import (
    GeniusTui,
    Track,
    _extract_lyrics_containers,
    fmt_time,
    parse_lrc,
    terminal_prefers_light_theme,
)


def test_parse_lrc_basic():
    lrc = "[00:26.83] Karma police\n[01:03.26]Is making me feel ill"
    assert parse_lrc(lrc) == [
        (26.83, "Karma police"),
        (63.26, "Is making me feel ill"),
    ]


def test_parse_lrc_multiple_stamps_and_colon_fraction():
    parsed = parse_lrc("[00:12:5][00:14.100]dual stamp")
    assert [ln for _, ln in parsed] == ["dual stamp", "dual stamp"]
    assert parsed[0][0] == pytest.approx(12.5)
    assert parsed[1][0] == pytest.approx(14.1)


def test_parse_lrc_ignores_metadata_and_sorts():
    parsed = parse_lrc("[ti:title]\n[00:10.0]b\n[00:05.0]a")
    assert parsed == [(5.0, "a"), (10.0, "b")]


def test_extract_lyrics_containers():
    page = (
        '<html><div data-lyrics-container="true" class="x">'
        '<a href="#">Karma police</a><br/>Arrest this &amp; man'
        '<div class="inline"><span>nested</span></div>'
        "<br><br><br>Next line</div><div>junk</div>"
    )
    text = _extract_lyrics_containers(page)
    assert "Karma police" in text
    assert "Arrest this & man" in text
    assert "nested" in text
    assert "junk" not in text
    assert "\n\n\n" not in text


def test_track_position_extrapolation():
    track = Track(title="a", artist="b", position=10.0, playing=True)
    time.sleep(0.05)
    assert 10.0 < track.position_now() < 10.5
    assert track.position_now(offset=-20) == 0.0
    paused = Track(title="a", artist="b", position=10.0, playing=False)
    time.sleep(0.05)
    assert paused.position_now() == 10.0


def test_fmt_time():
    assert fmt_time(261.9) == "4:21"
    assert fmt_time(-3) == "0:00"


def test_terminal_prefers_light_theme_from_colorfgbg(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    monkeypatch.delenv("APPLE_INTERFACE_STYLE", raising=False)
    assert terminal_prefers_light_theme()

    monkeypatch.setenv("COLORFGBG", "15;0")
    assert not terminal_prefers_light_theme()


def test_terminal_prefers_light_theme_from_macos_appearance(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("APPLE_INTERFACE_STYLE", "Dark")
    assert not terminal_prefers_light_theme()

    monkeypatch.setenv("APPLE_INTERFACE_STYLE", "Light")
    assert terminal_prefers_light_theme()


def test_terminal_prefers_light_theme_defaults_to_light_on_macos(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.delenv("APPLE_INTERFACE_STYLE", raising=False)
    monkeypatch.setattr("genius_tui.app.platform.system", lambda: "Darwin")
    assert terminal_prefers_light_theme()


def test_terminal_prefers_light_theme_malformed_colorfgbg_falls_back(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "foo")
    monkeypatch.setenv("APPLE_INTERFACE_STYLE", "Dark")
    assert not terminal_prefers_light_theme()


def test_terminal_prefers_light_theme_malformed_colorfgbg_no_fallback(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "foo")
    monkeypatch.delenv("APPLE_INTERFACE_STYLE", raising=False)
    assert isinstance(terminal_prefers_light_theme(), bool)


@pytest.mark.anyio
async def test_lyrics_only_toggle_hides_chrome():
    app = GeniusTui()
    async with app.run_test():
        lyrics = app.query_one("#lyrics", VerticalScroll)
        assert app.query_one("#header", Static).display
        assert app.query_one("#status", Static).display
        assert app.query_one("#footer", Footer).display
        assert lyrics.show_vertical_scrollbar
        assert not lyrics.has_class("lyrics-only")

        app.action_toggle_lyrics_only()
        assert not app.query_one("#header", Static).display
        assert not app.query_one("#status", Static).display
        assert not app.query_one("#footer", Footer).display
        assert not lyrics.show_vertical_scrollbar
        assert lyrics.has_class("lyrics-only")

        app.action_toggle_lyrics_only()
        assert app.query_one("#header", Static).display
        assert app.query_one("#status", Static).display
        assert app.query_one("#footer", Footer).display
        assert lyrics.show_vertical_scrollbar
        assert not lyrics.has_class("lyrics-only")
