"""Tests for the auto-update CI gating in bot.py."""
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

    def get(self, url):
        return self._response


@pytest.mark.asyncio
async def test_ci_passed_all_green():
    session = _FakeSession(200, {'check_runs': [
        {'status': 'completed', 'conclusion': 'success'},
        {'status': 'completed', 'conclusion': 'skipped'},
    ]})
    assert await bot._ci_passed(session, 'owner/repo', 'a' * 40) is True


@pytest.mark.asyncio
async def test_ci_passed_failure_blocks():
    session = _FakeSession(200, {'check_runs': [
        {'status': 'completed', 'conclusion': 'success'},
        {'status': 'completed', 'conclusion': 'failure'},
    ]})
    assert await bot._ci_passed(session, 'owner/repo', 'a' * 40) is False


@pytest.mark.asyncio
async def test_ci_passed_in_progress_blocks():
    session = _FakeSession(200, {'check_runs': [
        {'status': 'in_progress', 'conclusion': None},
    ]})
    assert await bot._ci_passed(session, 'owner/repo', 'a' * 40) is False


@pytest.mark.asyncio
async def test_ci_passed_no_runs_blocks():
    session = _FakeSession(200, {'check_runs': []})
    assert await bot._ci_passed(session, 'owner/repo', 'a' * 40) is False


@pytest.mark.asyncio
async def test_ci_passed_http_error_blocks():
    session = _FakeSession(503, {})
    assert await bot._ci_passed(session, 'owner/repo', 'a' * 40) is False
