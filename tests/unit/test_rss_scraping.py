"""Tests for the RSS event-page scraping path.

Covers JSON-LD extraction from HTML and aiohttp session interaction.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from commands import EventFeed


def _async_response(status=200, text=''):
    """Build a mock that supports `async with session.get(url) as response`."""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    response.raise_for_status = MagicMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _session_returning(*responses):
    """Build a session whose get() returns the given responses in order."""
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


# ── _scrape_event_page ──

@pytest.mark.asyncio
async def test_scrape_extracts_single_jsonld_event():
    html = '''<html><head>
<script type="application/ld+json">
{"@type":"Event","name":"Test","startDate":"2099-06-01T18:00:00Z",
 "location":"Some Venue"}
</script></head></html>'''
    session = _session_returning(_async_response(text=html))
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(session, 'http://x', 'uid-1')

    assert result is not None
    assert result['summary'] == 'Test'
    assert result['location'] == 'Some Venue'


@pytest.mark.asyncio
async def test_scrape_handles_jsonld_array():
    html = '''<script type="application/ld+json">
[{"@type":"WebPage","name":"page"},
 {"@type":"Event","name":"Real Event",
  "startDate":"2099-07-01T18:00:00Z","location":"Hall"}]
</script>'''
    session = _session_returning(_async_response(text=html))
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(session, 'http://x', 'uid-1')

    assert result is not None and result['summary'] == 'Real Event'


@pytest.mark.asyncio
async def test_scrape_returns_none_when_no_event_jsonld():
    html = '''<script type="application/ld+json">
{"@type":"Organization","name":"Acme"}
</script>'''
    session = _session_returning(_async_response(text=html))
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(session, 'http://x', 'uid-1')

    assert result is None


@pytest.mark.asyncio
async def test_scrape_returns_none_on_http_error():
    session = _session_returning(_async_response(status=404))
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(session, 'http://x', 'uid-1')

    assert result is None


@pytest.mark.asyncio
async def test_scrape_skips_invalid_json_blocks():
    """One bad JSON block + one good one — should return the good one."""
    html = '''<script type="application/ld+json">{invalid json}</script>
<script type="application/ld+json">
{"@type":"Event","name":"Good",
 "startDate":"2099-06-01T18:00:00Z"}
</script>'''
    session = _session_returning(_async_response(text=html))
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(session, 'http://x', 'uid-1')

    assert result is not None and result['summary'] == 'Good'


@pytest.mark.asyncio
async def test_scrape_empty_url_returns_none():
    ef = EventFeed.__new__(EventFeed)
    result = await ef._scrape_event_page(MagicMock(), '', 'uid-1')
    assert result is None
