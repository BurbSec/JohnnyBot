"""Graceful-failure coverage: a discord.Forbidden (or any other error)
raised inside a command must always produce a Discord-side response,
never an unhandled exception or a masked InteractionResponded."""
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord
from discord import app_commands

import commands


def _interaction(is_done=False, moderator=False):
    inter = MagicMock()
    inter.user = MagicMock()
    inter.user.guild_permissions = discord.Permissions(
        manage_messages=moderator, administrator=False)
    inter.response.is_done = MagicMock(return_value=is_done)
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


# ---------------------------------------------------------------------
# _command_error_handler
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forbidden_wrapped_in_command_invoke_error_gets_permission_message():
    """This is the real-world shape: app_commands wraps any exception
    raised inside a command body (e.g. an unguarded channel.send()
    hitting Forbidden) in CommandInvokeError before on_error sees it."""
    inter = _interaction()
    original = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'missing access')
    wrapped = app_commands.errors.CommandInvokeError(MagicMock(), original)

    await commands._command_error_handler(inter, wrapped)  # pylint: disable=protected-access

    msg = inter.response.send_message.call_args.args[0]
    assert "don't have the Discord server permissions" in msg
    assert 'Error:' not in msg


@pytest.mark.asyncio
async def test_bare_forbidden_gets_permission_message():
    inter = _interaction()
    error = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'missing access')

    await commands._command_error_handler(inter, error)  # pylint: disable=protected-access

    msg = inter.response.send_message.call_args.args[0]
    assert "don't have the Discord server permissions" in msg


@pytest.mark.asyncio
async def test_missing_permissions_still_reports_specific_permission():
    inter = _interaction()
    error = app_commands.errors.MissingPermissions(['manage_messages'])

    await commands._command_error_handler(inter, error)  # pylint: disable=protected-access

    msg = inter.response.send_message.call_args.args[0]
    assert 'Manage Messages' in msg


@pytest.mark.asyncio
async def test_error_handler_uses_followup_when_already_responded():
    """A command that deferred (or already responded) before failing
    must get its error via followup, not response.send_message, or the
    user is left with nothing but a dead spinner."""
    inter = _interaction(is_done=True)
    error = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'x')

    await commands._command_error_handler(inter, error)  # pylint: disable=protected-access

    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_handler_survives_broken_log_read(monkeypatch):
    """get_last_log_line() runs outside the response try/except; if it
    raises, the user must still get a response instead of nothing."""
    inter = _interaction(moderator=True)
    monkeypatch.setattr(commands, '_is_moderator', lambda _u: True)

    def _broken_log():
        raise OSError('log file vanished')
    monkeypatch.setattr(commands, 'get_last_log_line', _broken_log)

    await commands._command_error_handler(inter, RuntimeError('boom'))  # pylint: disable=protected-access

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args.args[0]
    assert 'Last log' not in msg


# ---------------------------------------------------------------------
# _send_or_followup
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_or_followup_uses_response_when_fresh():
    inter = _interaction(is_done=False)
    await commands._send_or_followup(inter, 'hi')  # pylint: disable=protected-access
    inter.response.send_message.assert_awaited_once()
    inter.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_or_followup_uses_followup_when_already_responded():
    """This is exactly the bug dashboard_command had: its confirm branch
    already used response.send_message, so a later failure must not try
    to send_message again (InteractionResponded) — it must fall back to
    followup instead."""
    inter = _interaction(is_done=True)
    await commands._send_or_followup(inter, 'hi')  # pylint: disable=protected-access
    inter.followup.send.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


# ---------------------------------------------------------------------
# _tree_error_handler (tree-wide backstop)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tree_error_handler_responds_when_nothing_handled_it():
    """The whole point: a command registered without error= (today's
    gap, or a future command #31 someone forgets to wire up) must still
    get a Discord-side response via this backstop."""
    inter = _interaction(is_done=False)
    error = discord.Forbidden(MagicMock(status=403, reason='x'), 'x')

    await commands._tree_error_handler(inter, error)  # pylint: disable=protected-access

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args.args[0]
    assert "don't have the Discord server permissions" in msg


@pytest.mark.asyncio
async def test_tree_error_handler_noop_when_already_responded():
    """discord.py calls tree.on_error unconditionally after any
    per-command error= handler, even one that already succeeded — the
    backstop must not send a second, duplicate error message."""
    inter = _interaction(is_done=True)
    error = discord.Forbidden(MagicMock(status=403, reason='x'), 'x')

    await commands._tree_error_handler(inter, error)  # pylint: disable=protected-access

    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_not_awaited()


# ---------------------------------------------------------------------
# list_event_feeds_command
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_event_feeds_caps_fields_at_discord_limit(monkeypatch):
    """Discord embeds cap at 25 fields; a guild with more feeds than
    that must not raise an unhandled HTTPException."""
    guild = MagicMock()
    guild.id = 1
    feeds = {
        f'https://example.com/feed{i}.ics': {
            'name': f'Feed {i}', 'feed_type': 'ical', 'channel': 'events'}
        for i in range(30)
    }
    monkeypatch.setattr(commands, 'event_feed', MagicMock(feeds={1: feeds}))

    inter = MagicMock()
    inter.guild = guild
    inter.response.send_message = AsyncMock()

    await commands.list_event_feeds_command(inter)

    inter.response.send_message.assert_awaited_once()
    embed = inter.response.send_message.call_args.kwargs['embed']
    assert len(embed.fields) <= 25


@pytest.mark.asyncio
async def test_list_event_feeds_reports_forbidden_gracefully(monkeypatch):
    guild = MagicMock()
    guild.id = 1
    feeds = {'https://example.com/a.ics': {'name': 'A', 'feed_type': 'ical', 'channel': 'events'}}
    monkeypatch.setattr(commands, 'event_feed', MagicMock(feeds={1: feeds}))

    inter = MagicMock()
    inter.guild = guild
    # The failing send_message never actually responds, so
    # _send_or_followup's is_done() check must still say False —
    # otherwise the fallback would (wrongly) go to followup instead.
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock(
        side_effect=[discord.Forbidden(MagicMock(status=403, reason='x'), 'x'), None])
    inter.followup.send = AsyncMock()

    await commands.list_event_feeds_command(inter)

    assert inter.response.send_message.await_count == 2
    inter.followup.send.assert_not_awaited()
    final_msg = inter.response.send_message.call_args.args[0]
    assert "don't have permission" in final_msg
