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


# --- dependency sync on auto-update -----------------------------------

@pytest.fixture
def in_venv(monkeypatch):
    monkeypatch.setattr(bot.sys, 'prefix', '/srv/johnnybot/.venv')
    monkeypatch.setattr(bot.sys, 'base_prefix', '/usr')


@pytest.mark.asyncio
async def test_sync_dependencies_without_uv_asks_for_manual_fix(monkeypatch, in_venv):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: None)
    error = await bot._sync_dependencies()
    assert 'not installed' in error and 'uv sync' in error


@pytest.mark.asyncio
async def test_sync_dependencies_refuses_system_python(monkeypatch):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/uv')
    monkeypatch.setattr(bot.sys, 'prefix', '/usr')
    monkeypatch.setattr(bot.sys, 'base_prefix', '/usr')

    async def must_not_run(*args, **kwargs):
        raise AssertionError('uv must not run against a system Python')
    monkeypatch.setattr(bot, '_run_cmd', must_not_run)

    error = await bot._sync_dependencies()
    assert 'virtual environment' in error


@pytest.mark.asyncio
async def test_sync_dependencies_targets_running_venv(monkeypatch, in_venv):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/uv')
    calls = []

    async def fake_run(*args, env=None):
        calls.append((args, env))
        return 0, '', ''
    monkeypatch.setattr(bot, '_run_cmd', fake_run)

    assert await bot._sync_dependencies() is None
    (args, env), = calls
    assert args == ('/usr/bin/uv', 'sync', '--frozen', '--no-dev')
    assert env['UV_PROJECT_ENVIRONMENT'] == '/srv/johnnybot/.venv'


@pytest.mark.asyncio
async def test_sync_dependencies_reports_uv_failure(monkeypatch, in_venv):
    monkeypatch.setattr(bot.shutil, 'which', lambda name: '/usr/bin/uv')

    async def fake_run(*args, env=None):
        return 2, '', 'lockfile needs to be updated'
    monkeypatch.setattr(bot, '_run_cmd', fake_run)

    error = await bot._sync_dependencies()
    assert 'lockfile needs to be updated' in error


@pytest.mark.asyncio
async def test_auto_update_bails_before_restart_when_sync_fails(monkeypatch):
    async def fake_run(*args, env=None):
        return 0, '', ''  # git pull succeeds
    async def failing_sync():
        return 'sync broke'
    def no_exec(*args):
        raise AssertionError('must not restart with unsynced dependencies')
    monkeypatch.setattr(bot, '_run_cmd', fake_run)
    monkeypatch.setattr(bot, '_sync_dependencies', failing_sync)
    monkeypatch.setattr(bot.os, 'execv', no_exec)

    error = await bot._auto_update_and_restart('a' * 40, 'b' * 40, True)
    assert error == 'sync broke'
