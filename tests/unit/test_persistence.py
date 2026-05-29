"""Persistence round-trip tests (atomic JSON writes, feed serialization)."""
import os
import json
import threading
from datetime import datetime
from unittest.mock import patch

from commands import _atomic_json_write, EventFeed


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / 'out.json'
    _atomic_json_write(str(target), {'a': 1})
    assert target.exists()
    assert json.loads(target.read_text()) == {'a': 1}


def test_atomic_write_overwrites(tmp_path):
    target = tmp_path / 'out.json'
    _atomic_json_write(str(target), {'first': True})
    _atomic_json_write(str(target), {'second': True})
    assert json.loads(target.read_text()) == {'second': True}


def test_atomic_write_no_temp_files_left(tmp_path):
    target = tmp_path / 'out.json'
    _atomic_json_write(str(target), {'a': 1})
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith('.tmp')]
    assert leftovers == []


def test_atomic_write_failure_cleans_temp(tmp_path):
    target = tmp_path / 'out.json'
    # json.dump will fail on a set
    try:
        _atomic_json_write(str(target), {'bad': {1, 2, 3}})
    except TypeError:
        pass
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith('.tmp')]
    assert leftovers == []
    assert not target.exists()


def test_feed_save_then_load_roundtrip(tmp_path, monkeypatch):
    feeds_file = str(tmp_path / 'feeds.json')
    monkeypatch.setattr('commands.FEEDS_FILE', feeds_file)

    ef = EventFeed.__new__(EventFeed)
    ef.feeds = {
        12345: {
            'http://example.com/feed.ics': {
                'name': 'demo',
                'last_checked': datetime(2026, 1, 1, 12, 0, 0),
                'channel': 'events',
                'posted_events': {'uid1|2026-01-15', 'uid2|2026-01-20'},
                'feed_type': 'ical',
            }
        }
    }
    ef._feeds_lock = threading.Lock()
    ef.save_feeds()

    # Reload into a fresh instance
    ef2 = EventFeed.__new__(EventFeed)
    ef2.feeds = {}
    ef2._load_feeds()
    assert 12345 in ef2.feeds
    feed = ef2.feeds[12345]['http://example.com/feed.ics']
    assert feed['name'] == 'demo'
    assert isinstance(feed['posted_events'], set)
    assert feed['posted_events'] == {'uid1|2026-01-15', 'uid2|2026-01-20'}
    assert isinstance(feed['last_checked'], datetime)
    assert feed['last_checked'].year == 2026
