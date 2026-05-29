"""Autoreply add/list/remove/toggle + message matching."""
import threading
from unittest.mock import MagicMock, AsyncMock

import pytest

import commands


@pytest.fixture(autouse=True)
def fresh_autoreplies(tmp_path, monkeypatch):
    """Each test gets an empty autoreplies dict + isolated JSON file."""
    monkeypatch.setattr(commands, 'autoreplies', {})
    monkeypatch.setattr(commands, 'autoreplies_lock', threading.Lock())
    monkeypatch.setattr(commands, 'AUTOREPLIES_FILE', str(tmp_path / 'ar.json'))
    yield


def _interaction(guild_id=1):
    inter = MagicMock()
    inter.guild = MagicMock()
    inter.guild.id = guild_id
    inter.user = MagicMock()
    inter.user.id = 42
    inter.response.send_message = AsyncMock()
    return inter


def _message(content, guild_id=1, is_bot=False):
    msg = MagicMock()
    msg.content = content
    msg.author = MagicMock()
    msg.author.bot = is_bot
    msg.guild = MagicMock()
    msg.guild.id = guild_id
    msg.reply = AsyncMock()
    return msg


# ── add ──

@pytest.mark.asyncio
async def test_add_creates_rule():
    inter = _interaction()
    await commands.autoreply_add_command(inter, 'hello', 'hi there')
    assert len(commands.autoreplies) == 1
    rule = next(iter(commands.autoreplies.values()))
    assert rule['trigger_string'] == 'hello'
    assert rule['reply_string'] == 'hi there'
    assert rule['enabled'] is True
    inter.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_add_rejects_empty_trigger():
    inter = _interaction()
    await commands.autoreply_add_command(inter, '   ', 'reply')
    assert len(commands.autoreplies) == 0


@pytest.mark.asyncio
async def test_add_rejects_oversize_reply():
    inter = _interaction()
    await commands.autoreply_add_command(inter, 't', 'x' * 3000)
    assert len(commands.autoreplies) == 0


# ── matching ──

@pytest.mark.asyncio
async def test_match_triggers_reply_case_insensitive():
    commands.autoreplies['r1'] = {
        'trigger_string': 'hello', 'reply_string': 'hi',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    msg = _message("Hello world")
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_awaited_once_with('hi', mention_author=False)


@pytest.mark.asyncio
async def test_match_case_sensitive_no_match():
    commands.autoreplies['r1'] = {
        'trigger_string': 'Hello', 'reply_string': 'hi',
        'guild_id': 1, 'enabled': True, 'case_sensitive': True,
    }
    msg = _message("hello world")
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_rule_does_not_fire():
    commands.autoreplies['r1'] = {
        'trigger_string': 'hello', 'reply_string': 'hi',
        'guild_id': 1, 'enabled': False, 'case_sensitive': False,
    }
    msg = _message("hello")
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_other_guild_rule_does_not_fire():
    commands.autoreplies['r1'] = {
        'trigger_string': 'hello', 'reply_string': 'hi',
        'guild_id': 999, 'enabled': True, 'case_sensitive': False,
    }
    msg = _message("hello", guild_id=1)
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_bot_messages_ignored():
    commands.autoreplies['r1'] = {
        'trigger_string': 'hello', 'reply_string': 'hi',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    msg = _message("hello", is_bot=True)
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_first_matching_rule_wins():
    commands.autoreplies['r1'] = {
        'trigger_string': 'foo', 'reply_string': 'first',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    commands.autoreplies['r2'] = {
        'trigger_string': 'foo', 'reply_string': 'second',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    msg = _message("foo bar")
    await commands.check_message_for_autoreplies(msg)
    msg.reply.assert_awaited_once_with('first', mention_author=False)


# ── remove + toggle ──

@pytest.mark.asyncio
async def test_remove_drops_rule():
    commands.autoreplies['r1'] = {
        'trigger_string': 'x', 'reply_string': 'y',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    inter = _interaction()
    await commands.autoreply_remove_command(inter, 'r1')
    assert 'r1' not in commands.autoreplies


@pytest.mark.asyncio
async def test_toggle_flips_enabled():
    commands.autoreplies['r1'] = {
        'trigger_string': 'x', 'reply_string': 'y',
        'guild_id': 1, 'enabled': True, 'case_sensitive': False,
    }
    inter = _interaction()
    await commands.autoreply_toggle_command(inter, 'r1')
    assert commands.autoreplies['r1']['enabled'] is False
    await commands.autoreply_toggle_command(inter, 'r1')
    assert commands.autoreplies['r1']['enabled'] is True
