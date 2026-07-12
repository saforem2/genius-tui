import time

import pytest

from genius_tui.app import Track, _extract_lyrics_containers, fmt_time, parse_lrc


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
