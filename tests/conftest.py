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
    cfg.MODERATOR_ROLE_NAME = 'Moderators'
    cfg.VOICE_CHAPERONE_ENABLED = True
    cfg.ADULT_ROLE_NAMES = {'Dads', 'GrownUps'}
    cfg.CHILD_ROLE_NAMES = {'Kids', 'Bambinos'}
    cfg.UPDATE_CHECKING_ENABLED = False
    cfg.UPDATE_CHECK_REPO_URL = 'https://github.com/example/repo'
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
