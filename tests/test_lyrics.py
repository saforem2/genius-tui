import base64
import io
import time

import pytest
from PIL import Image

from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from genius_tui.app import (
    GeniusTui,
    Track,
    _extract_lyrics_containers,
    decode_album_art_image,
    fetch_genius_url,
    fmt_delay,
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


def test_decode_album_art_image():
    image = Image.new("RGB", (2, 2), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    artwork = base64.b64encode(buffer.getvalue()).decode()
    decoded = decode_album_art_image(artwork)
    assert decoded is not None
    assert decoded.size == (2, 2)


def test_decode_album_art_image_handles_invalid_data():
    assert decode_album_art_image("not base64") is None


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
async def test_invalid_repeated_artwork_stays_hidden():
    app = GeniusTui()
    async with app.run_test():
        track = Track(title="a", artist="b", artwork_data="AAAA")
        app.update_album_art(track)
        assert not app.query_one("#album-art").display
        app.update_album_art(track)
        assert not app.query_one("#album-art").display


@pytest.mark.anyio
async def test_lyrics_only_toggle_hides_chrome():
    app = GeniusTui()
    async with app.run_test():
        lyrics = app.query_one("#lyrics", VerticalScroll)
        assert app.query_one("#title", Static).display
        assert app.query_one("#source", Static).display
        assert app.query_one("#footer", Footer).display
        assert not lyrics.has_class("scrollbar-visible")
        assert not lyrics.has_class("lyrics-only")

        app.show_scrollbar_temporarily()
        assert lyrics.has_class("scrollbar-visible")
        app.hide_scrollbar()
        assert not lyrics.has_class("scrollbar-visible")

        app.action_toggle_footer()
        assert not app.query_one("#footer", Footer).display
        app.action_toggle_footer()
        assert app.query_one("#footer", Footer).display

        app.action_toggle_lyrics_only()
        assert not app.query_one("#top").display
        assert not app.query_one("#footer", Footer).display
        assert not lyrics.has_class("scrollbar-visible")
        assert lyrics.has_class("lyrics-only")

        app.action_toggle_lyrics_only()
        assert app.query_one("#top").display
        assert app.query_one("#footer", Footer).display

        assert not lyrics.has_class("scrollbar-visible")
        assert not lyrics.has_class("lyrics-only")

        app.action_toggle_footer()
        assert not app.query_one("#footer", Footer).display
        app.action_toggle_lyrics_only()
        assert not app.query_one("#top").display
        assert not app.query_one("#footer", Footer).display
        app.action_toggle_lyrics_only()
        assert app.query_one("#top").display
        assert not app.query_one("#footer", Footer).display


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return _FakeResponse(self._payload)

    async def aclose(self):
        return None


@pytest.mark.anyio
async def test_fetch_genius_url_returns_song_url():
    payload = {
        "response": {
            "sections": [
                {
                    "hits": [
                        {"type": "song", "result": {"url": "https://genius.com/song"}},
                    ]
                }
            ]
        }
    }
    client = _FakeClient(payload)
    url = await fetch_genius_url(client, Track(title="a", artist="b"))
    assert url == "https://genius.com/song"
    assert client.calls == [
        ("https://genius.com/api/search/multi", {"q": "b a"})
    ]


@pytest.mark.anyio
async def test_fetch_genius_url_returns_none_when_no_song():
    client = _FakeClient({"response": {"sections": []}})
    url = await fetch_genius_url(client, Track(title="a", artist="b"))
    assert url is None


@pytest.mark.anyio
async def test_load_genius_url_sets_url_for_current_track():
    payload = {
        "response": {
            "sections": [
                {"hits": [{"type": "song", "result": {"url": "https://genius.com/x"}}]}
            ]
        }
    }
    app = GeniusTui()
    async with app.run_test():
        app.client = _FakeClient(payload)
        track = Track(title="a", artist="b")
        app.track = track
        await app.load_genius_url(track)
        assert app.genius_url == "https://genius.com/x"


@pytest.mark.anyio
async def test_load_genius_url_ignores_stale_track():
    payload = {
        "response": {
            "sections": [
                {"hits": [{"type": "song", "result": {"url": "https://genius.com/x"}}]}
            ]
        }
    }
    app = GeniusTui()
    async with app.run_test():
        app.client = _FakeClient(payload)
        app.track = Track(title="new", artist="new")
        await app.load_genius_url(Track(title="old", artist="old"))
        assert app.genius_url is None


@pytest.mark.anyio
async def test_open_genius_uses_cached_url(monkeypatch):
    app = GeniusTui()
    async with app.run_test():
        opened = []

        def fake_open(url):
            opened.append(url)
            return True

        monkeypatch.setattr("genius_tui.app.webbrowser.open", fake_open)
        app.genius_url = "https://genius.com/song"
        app.action_open_genius()
        assert opened == ["https://genius.com/song"]


@pytest.mark.anyio
async def test_open_genius_warns_when_browser_fails(monkeypatch):
    app = GeniusTui()
    async with app.run_test():
        notes = []
        monkeypatch.setattr("genius_tui.app.webbrowser.open", lambda url: False)
        monkeypatch.setattr(
            type(app), "notify",
            lambda self, msg, **kw: notes.append((msg, kw.get("severity"))),
        )
        app.genius_url = "https://genius.com/song"
        app.action_open_genius()
        assert notes and notes[0][1] == "warning"


def test_fmt_delay_zero_is_blank():
    assert fmt_delay(0.0) == ""


def test_fmt_delay_shows_sign_and_one_decimal():
    assert fmt_delay(1.5) == "delay +1.5s"
    assert fmt_delay(-0.5) == "delay -0.5s"


@pytest.mark.anyio
async def test_action_offset_updates_delay_indicator():
    app = GeniusTui()
    async with app.run_test():
        delay = app.query_one("#delay", Static)
        assert app.offset == 0.0
        assert str(delay.render()) == ""

        app.action_offset(0.5)
        assert app.offset == 0.5
        assert str(delay.render()) == "delay +0.5s"

        app.action_offset(-1.0)
        assert app.offset == -0.5
        assert str(delay.render()) == "delay -0.5s"

        app.action_offset(0.5)
        assert app.offset == 0.0
        assert str(delay.render()) == ""
