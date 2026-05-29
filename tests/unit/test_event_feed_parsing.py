"""Pure-function tests for EventFeed parsing logic."""
from datetime import datetime, timedelta

import pytest
from icalendar import Calendar

from commands import EventFeed


# ── _detect_feed_type ──

def test_detect_ical_by_body():
    text = "BEGIN:VCALENDAR\nEND:VCALENDAR"
    assert EventFeed._detect_feed_type(text) == 'ical'


def test_detect_ical_by_content_type():
    assert EventFeed._detect_feed_type("", "text/calendar") == 'ical'


def test_detect_rss_by_body():
    assert EventFeed._detect_feed_type("<rss version='2.0'>") == 'rss'


def test_detect_atom_by_content_type():
    assert EventFeed._detect_feed_type("<feed>", "application/atom+xml") == 'rss'


def test_detect_defaults_to_ical():
    assert EventFeed._detect_feed_type("random text") == 'ical'


# ── _strip_html_tags ──

def test_strip_html_basic():
    assert EventFeed._strip_html_tags("<p>hello</p>") == "hello"


def test_strip_html_br_becomes_newline():
    out = EventFeed._strip_html_tags("a<br>b<br/>c")
    assert out == "a\nb\nc"


def test_strip_html_unescapes_entities():
    assert EventFeed._strip_html_tags("&amp;&lt;") == "&<"


def test_strip_html_empty():
    assert EventFeed._strip_html_tags("") == ""
    assert EventFeed._strip_html_tags(None) == ""


# ── _strip_urls ──

def test_strip_urls_removes_url():
    out = EventFeed._strip_urls("Venue at https://example.com/foo today")
    assert "https" not in out and "Venue at" in out


def test_strip_urls_collapses_whitespace():
    out = EventFeed._strip_urls("a   b  https://x.y   c")
    assert out == "a b c"


# ── _parse_iso_date ──

def test_parse_iso_date_with_z():
    d = EventFeed._parse_iso_date("2025-01-15T18:00:00Z")
    assert d is not None
    assert d.year == 2025 and d.month == 1 and d.day == 15


def test_parse_iso_date_date_only():
    d = EventFeed._parse_iso_date("2025-01-15")
    assert d is not None and d.year == 2025


def test_parse_iso_date_empty():
    assert EventFeed._parse_iso_date("") is None


def test_parse_iso_date_invalid():
    assert EventFeed._parse_iso_date("not-a-date") is None


# ── _extract_ical_event & _parse_calendar_events ──

@pytest.fixture
def sample_calendar():
    """Build an in-memory iCal with events at known dates relative to now."""
    soon = (datetime.now() + timedelta(days=5)).strftime('%Y%m%dT180000Z')
    later = (datetime.now() + timedelta(days=10)).strftime('%Y%m%dT180000Z')
    ical = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:future-1@example.com
SUMMARY:Future Event A
DTSTART:{soon}
DTEND:{soon}
END:VEVENT
BEGIN:VEVENT
UID:future-2@example.com
SUMMARY:Future Event B
DTSTART:{later}
DTEND:{later}
END:VEVENT
BEGIN:VEVENT
UID:past@example.com
SUMMARY:Past Event Should Be Filtered
DTSTART:20200101T180000Z
DTEND:20200101T200000Z
END:VEVENT
END:VCALENDAR
"""
    return Calendar.from_ical(ical)


def test_parse_calendar_filters_past_events(sample_calendar):
    ef = EventFeed.__new__(EventFeed)
    events = ef._parse_calendar_events(
        sample_calendar, {'posted_events': set()})
    titles = [e['summary'] for e in events]
    assert "Past Event Should Be Filtered" not in titles


def test_parse_calendar_skips_already_posted(sample_calendar):
    ef = EventFeed.__new__(EventFeed)
    events = ef._parse_calendar_events(
        sample_calendar, {'posted_events': set()})
    assert events  # baseline
    # Now mark the first event as already posted
    already = {events[0]['uid']}
    events2 = ef._parse_calendar_events(
        sample_calendar, {'posted_events': already})
    assert events[0]['uid'] not in {e['uid'] for e in events2}


def test_parse_calendar_assigns_composite_uid(sample_calendar):
    ef = EventFeed.__new__(EventFeed)
    events = ef._parse_calendar_events(
        sample_calendar, {'posted_events': set()})
    for ev in events:
        assert '|' in ev['uid']  # uid|YYYY-MM-DD


# ── _cleanup_old_posted_events ──

def test_cleanup_keeps_recent_drops_old(tmp_path, monkeypatch):
    # Build an EventFeed without invoking __init__'s file I/O
    ef = EventFeed.__new__(EventFeed)
    import threading
    ef.feeds = {
        1: {
            'http://x': {
                'posted_events': {
                    f'uid1|{(datetime.now()).strftime("%Y-%m-%d")}',   # recent
                    'uid2|2020-01-01',                                  # old
                    'uid3-legacy',                                      # legacy (no date)
                },
            },
        },
    }
    ef._feeds_lock = threading.Lock()
    # Stub save_feeds so we don't touch disk
    ef.save_feeds = lambda: None

    ef._cleanup_old_posted_events()
    remaining = ef.feeds[1]['http://x']['posted_events']
    # Recent should survive; old + legacy should be cleaned
    assert any(u.startswith('uid1|') for u in remaining)
    assert not any(u.startswith('uid2|') for u in remaining)
    assert 'uid3-legacy' not in remaining


# ── _parse_jsonld_event ──

def test_parse_jsonld_basic():
    ef = EventFeed.__new__(EventFeed)
    data = {
        '@type': 'Event',
        'name': 'Sample',
        'description': '<p>Hi</p>',
        'startDate': '2099-06-01T18:00:00Z',
        'endDate': '2099-06-01T20:00:00Z',
        'location': {'name': 'The Venue',
                     'address': {'streetAddress': '1 Main St'}},
    }
    out = ef._parse_jsonld_event(data, 'http://x', 'uid-1')
    assert out['summary'] == 'Sample'
    assert out['location'] == 'The Venue, 1 Main St'
    assert out['description'] == 'Hi'
    assert out['link'] == 'http://x'


def test_parse_jsonld_string_location():
    ef = EventFeed.__new__(EventFeed)
    data = {
        '@type': 'Event', 'name': 'X',
        'startDate': '2099-06-01T18:00:00Z',
        'location': 'Just a string venue',
    }
    out = ef._parse_jsonld_event(data, 'http://x', 'uid-1')
    assert out['location'] == 'Just a string venue'


def test_parse_jsonld_missing_start_returns_none():
    ef = EventFeed.__new__(EventFeed)
    out = ef._parse_jsonld_event(
        {'@type': 'Event', 'name': 'X'}, 'http://x', 'uid-1')
    assert out is None
