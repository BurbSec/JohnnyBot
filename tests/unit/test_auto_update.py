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


# --- changed-file gate ------------------------------------------------

class _RoutedSession:
    """Fake aiohttp session answering by URL substring."""

    def __init__(self, routes):
        self._routes = routes

    def get(self, url):
        for fragment, (status, payload) in self._routes.items():
            if fragment in url:
                return _FakeResponse(status, payload)
        raise AssertionError(f'unexpected request: {url}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _files(*names):
    return {'files': [{'filename': n} for n in names]}


@pytest.mark.asyncio
async def test_changed_files_lists_filenames():
    session = _RoutedSession({'/compare/': (200, _files('bot.py', 'README.md'))})
    assert await bot._changed_files(session, 'o/r', 'a', 'b') == ['bot.py', 'README.md']


@pytest.mark.asyncio
@pytest.mark.parametrize('status, payload', [
    (404, {}),                 # request failed
    (200, {}),                 # no file list at all
    (200, _files(*[f'f{i}.py' for i in range(bot._COMPARE_FILE_LIMIT)])),  # may be truncated
])
async def test_changed_files_unknown_is_none_not_empty(status, payload):
    session = _RoutedSession({'/compare/': (status, payload)})
    assert await bot._changed_files(session, 'o/r', 'a', 'b') is None


@pytest.fixture
def update_check(monkeypatch):
    """Drive check_for_updates with auto-update on and CI green.

    Returns a function taking the compare response; it runs one check
    and returns (auto_update_calls, notification_calls).
    """
    monkeypatch.setattr(bot.config, 'UPDATE_CHECKING_ENABLED', True)
    monkeypatch.setattr(bot.config, 'AUTO_UPDATE_ENABLED', True)
    monkeypatch.setattr(bot, '_last_notified_commit', None)

    async def fake_run(*args, **kwargs):
        return 0, 'a' * 40, ''  # git rev-parse HEAD
    async def ci_green(*args):
        return True
    async def label(session, repo, sha, local):
        return 'v-local' if local else 'v-remote'
    monkeypatch.setattr(bot, '_run_cmd', fake_run)
    monkeypatch.setattr(bot, '_ci_passed', ci_green)
    monkeypatch.setattr(bot, '_version_label', label)

    async def run(compare_status, compare_payload):
        updates, notices = [], []

        async def fake_update(*args):
            updates.append(args)
            return 'stopped by test'
        async def fake_notify(*args):
            notices.append(args)
        monkeypatch.setattr(bot, '_auto_update_and_restart', fake_update)
        monkeypatch.setattr(bot, 'send_update_notification', fake_notify)
        monkeypatch.setattr(bot, '_get_moderators_channel', lambda: None)
        session = _RoutedSession({
            '/commits/main': (200, {'sha': 'b' * 40}),
            '/compare/': (compare_status, compare_payload),
        })
        monkeypatch.setattr(bot.aiohttp, 'ClientSession', lambda **kw: session)
        await bot.check_for_updates()
        return updates, notices

    return run


@pytest.mark.asyncio
async def test_failed_compare_blocks_auto_update(update_check):
    updates, notices = await update_check(502, {})
    assert updates == []
    (local, remote, config_changed, changes_unknown), = notices
    assert config_changed is False and changes_unknown is True


@pytest.mark.asyncio
async def test_truncated_compare_blocks_auto_update(update_check):
    many = _files(*[f'f{i}.py' for i in range(bot._COMPARE_FILE_LIMIT)])
    updates, notices = await update_check(200, many)
    assert updates == []
    assert notices[0][3] is True


@pytest.mark.asyncio
async def test_config_change_still_blocks_auto_update(update_check):
    updates, notices = await update_check(200, _files('config_example.py'))
    assert updates == []
    assert notices[0][2:] == (True, False)


@pytest.mark.asyncio
async def test_known_safe_change_auto_updates(update_check):
    updates, notices = await update_check(200, _files('bot.py'))
    assert len(updates) == 1
    assert notices == []

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

    error = await bot._auto_update_and_restart('a' * 40, 'b' * 40)
    assert error == 'sync broke'


@pytest.mark.asyncio
async def test_auto_update_syncs_even_without_a_known_dependency_change(monkeypatch):
    # The compare API's file list can be missing or truncated, so sync
    # must not depend on it
    synced = []
    async def fake_run(*args, env=None):
        return 0, '', ''
    async def fake_sync():
        synced.append(True)
        return None
    def fake_exec(*args):
        raise SystemExit('restarted')
    monkeypatch.setattr(bot, '_run_cmd', fake_run)
    monkeypatch.setattr(bot, '_sync_dependencies', fake_sync)
    monkeypatch.setattr(bot, '_get_moderators_channel', lambda: None)
    monkeypatch.setattr(bot.logging, 'shutdown', lambda: None)
    monkeypatch.setattr(bot.os, 'execv', fake_exec)

    with pytest.raises(SystemExit):
        await bot._auto_update_and_restart('a' * 40, 'b' * 40)
    assert synced == [True]
