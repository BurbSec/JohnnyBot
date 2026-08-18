"""DM auto-kick exemption and protected-channel enforcement must key off
the Manage Messages permission, not a role literally named
MODERATOR_ROLE_NAME — same authorization as the mod_only command gate,
and no role for a new server operator to create/name."""
from unittest.mock import MagicMock, AsyncMock

import pytest

import bot


def _member(manage_messages=False, member_id=1):
    m = MagicMock()
    m.id = member_id
    m.guild_permissions = MagicMock(manage_messages=manage_messages)
    m.kick = AsyncMock()
    return m


def _message(author, guild=None, channel_name='general', content='hi'):
    msg = MagicMock()
    msg.author = author
    msg.guild = guild
    msg.channel.name = channel_name
    msg.content = content
    msg.delete = AsyncMock()
    return msg


# ---------------------------------------------------------------------
# handle_unsolicited_dm
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dm_exempts_member_with_manage_messages(monkeypatch):
    mod = _member(manage_messages=True)
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=mod)
    # bot.bot.guilds is a read-only discord.py property; override it at
    # the class level so monkeypatch can still clean it up afterward.
    monkeypatch.setattr(type(bot.bot), 'guilds', property(lambda self: [guild]))
    monkeypatch.setattr(bot, '_was_recently_dmed', lambda _uid: False)

    msg = _message(author=mod, guild=None)

    await bot.handle_unsolicited_dm(msg)

    mod.kick.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_kicks_member_without_manage_messages(monkeypatch):
    author = _member(manage_messages=False)
    guild = MagicMock()
    guild.text_channels = []
    guild.get_member = MagicMock(return_value=author)
    author.guild = guild
    monkeypatch.setattr(type(bot.bot), 'guilds', property(lambda self: [guild]))
    monkeypatch.setattr(bot, '_was_recently_dmed', lambda _uid: False)

    msg = _message(author=author, guild=None)

    await bot.handle_unsolicited_dm(msg)

    author.kick.assert_awaited_once()


# ---------------------------------------------------------------------
# on_message protected-channel enforcement
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_protected_channel_spares_member_with_manage_messages(monkeypatch):
    monkeypatch.setattr(bot, 'PROTECTED_CHANNELS', {'rules'})
    monkeypatch.setattr(bot, '_check_autoreplies', AsyncMock())
    monkeypatch.setattr(bot.bot, 'process_commands', AsyncMock())

    mod = _member(manage_messages=True)
    mod.bot = False
    msg = _message(author=mod, guild=MagicMock(), channel_name='rules')

    await bot.on_message(msg)

    msg.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_protected_channel_deletes_message_without_manage_messages(monkeypatch):
    monkeypatch.setattr(bot, 'PROTECTED_CHANNELS', {'rules'})
    monkeypatch.setattr(bot, '_check_autoreplies', AsyncMock())
    monkeypatch.setattr(bot.bot, 'process_commands', AsyncMock())

    regular = _member(manage_messages=False)
    regular.bot = False
    msg = _message(author=regular, guild=MagicMock(), channel_name='rules')

    await bot.on_message(msg)

    msg.delete.assert_awaited_once()
