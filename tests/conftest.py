"""Shared test fixtures.

Stubs the `config` module before `commands` is imported so tests don't
require a real DISCORD_BOT_TOKEN or write a real log file.
"""
import os
import sys
import types
import logging
import tempfile

import pytest


def _install_config_stub():
    """Inject a fake `config` module into sys.modules."""
    if 'config' in sys.modules:
        return
    cfg = types.ModuleType('config')
    cfg.MODERATORS_CHANNEL_NAME = 'moderators_only'
    cfg.PROTECTED_CHANNELS = {'code_of_conduct'}
    cfg.VOICE_CHAPERONE_ENABLED = True
    cfg.ADULT_ROLE_NAMES = {'Dads', 'GrownUps'}
    cfg.CHILD_ROLE_NAMES = {'Kids', 'Bambinos'}
    cfg.RAID_PROTECTION_ENABLED = True
    cfg.RAID_JOIN_THRESHOLD = 6
    cfg.RAID_JOIN_WINDOW_SECONDS = 30
    cfg.RAID_LOCKDOWN_MINUTES = 60
    cfg.RAID_NEW_ACCOUNT_HOURS = 24
    cfg.ANTI_NUKE_ENABLED = True
    cfg.ANTI_NUKE_THRESHOLD = 3
    cfg.ANTI_NUKE_WINDOW_SECONDS = 60
    cfg.ANTI_NUKE_ACTION = 'strip_roles'
    cfg.SPAM_PROTECTION_ENABLED = True
    cfg.SPAM_CROSSPOST_WINDOW_SECONDS = 30
    cfg.SPAM_MENTION_THRESHOLD = 3
    cfg.SPAM_TIMEOUT_MINUTES = 10
    cfg.SPAM_NEW_ACCOUNT_DAYS = 2
    cfg.UPDATE_CHECKING_ENABLED = False
    cfg.UPDATE_CHECK_REPO_URL = 'https://github.com/example/repo'
    cfg.AUTO_UPDATE_ENABLED = False
    cfg.BOT_TIMEZONE = 'America/Chicago'
    cfg.TOKEN = 'test-token'

    tmp = tempfile.mkdtemp(prefix='johnnybot-test-')
    cfg.LOG_FILE = os.path.join(tmp, 'test.log')
    cfg.LOG_MAX_SIZE = 1024 * 1024
    cfg.REMINDERS_FILE = os.path.join(tmp, 'reminders.json')
    cfg.TEMP_DIR = os.path.join(tmp, 'temp')
    os.makedirs(cfg.TEMP_DIR, exist_ok=True)

    cfg.logger = logging.getLogger('johnnybot-test')
    cfg.logger.addHandler(logging.NullHandler())

    sys.modules['config'] = cfg


_install_config_stub()


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), 'fixtures')


@pytest.fixture
def tmp_json_dir(tmp_path):
    """Isolated directory for JSON persistence tests."""
    return tmp_path
