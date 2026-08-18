"""Tag-based version labels for the update checker.

Detection stays commit-SHA-based (see check_for_updates); these helpers
only resolve a friendlier display label, so a missing/unparsable tag
must fall back to the short SHA rather than blocking anything.
"""
from unittest.mock import AsyncMock

import pytest

import bot


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, status, payload):
        self._response = _FakeResponse(status, payload)

    def get(self, url):  # pylint: disable=unused-argument
        return self._response


def test_short_sha():
    assert bot._short_sha('a' * 40) == 'a' * 8  # pylint: disable=protected-access
    assert bot._short_sha(None) == '?'  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_local_tag_for_commit_found(monkeypatch):
    monkeypatch.setattr(bot, '_run_cmd', AsyncMock(return_value=(0, 'v1.0.0', '')))
    tag = await bot._local_tag_for_commit('a' * 40)  # pylint: disable=protected-access
    assert tag == 'v1.0.0'


@pytest.mark.asyncio
async def test_local_tag_for_commit_none(monkeypatch):
    monkeypatch.setattr(bot, '_run_cmd', AsyncMock(return_value=(0, '', '')))
    tag = await bot._local_tag_for_commit('a' * 40)  # pylint: disable=protected-access
    assert tag is None


@pytest.mark.asyncio
async def test_remote_tag_for_commit_found():
    sha = 'a' * 40
    session = _FakeSession(200, [{'name': 'v1.0.0', 'commit': {'sha': sha}}])
    tag = await bot._remote_tag_for_commit(session, 'owner/repo', sha)  # pylint: disable=protected-access
    assert tag == 'v1.0.0'


@pytest.mark.asyncio
async def test_remote_tag_for_commit_no_match():
    session = _FakeSession(200, [{'name': 'v1.0.0', 'commit': {'sha': 'b' * 40}}])
    tag = await bot._remote_tag_for_commit(session, 'owner/repo', 'a' * 40)  # pylint: disable=protected-access
    assert tag is None


@pytest.mark.asyncio
async def test_remote_tag_for_commit_http_error():
    session = _FakeSession(503, [])
    tag = await bot._remote_tag_for_commit(session, 'owner/repo', 'a' * 40)  # pylint: disable=protected-access
    assert tag is None


@pytest.mark.asyncio
async def test_version_label_falls_back_to_short_sha(monkeypatch):
    monkeypatch.setattr(bot, '_local_tag_for_commit', AsyncMock(return_value=None))
    sha = 'a' * 40
    label = await bot._version_label(None, 'owner/repo', sha, local=True)  # pylint: disable=protected-access
    assert label == sha[:8]


@pytest.mark.asyncio
async def test_version_label_prefers_tag(monkeypatch):
    monkeypatch.setattr(bot, '_local_tag_for_commit', AsyncMock(return_value='v2.3.4'))
    label = await bot._version_label(None, 'owner/repo', 'a' * 40, local=True)  # pylint: disable=protected-access
    assert label == 'v2.3.4'
