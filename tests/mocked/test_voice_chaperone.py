"""Voice chaperone alert logic."""
from unittest.mock import MagicMock, AsyncMock

import pytest

import bot


def _member(name, roles, is_bot=False):
    m = MagicMock()
    m.bot = is_bot
    m.display_name = name
    m.name = name
    m.roles = [MagicMock(name=r) for r in roles]
    # MagicMock's .name attr is tricky — set it explicitly
    for role_obj, role_name in zip(m.roles, roles):
        role_obj.name = role_name
    m.edit = AsyncMock()
    return m


def test_role_type_adult():
    m = _member("alice", ["Dads"])
    assert bot.get_user_role_type(m) == 'adult'


def test_role_type_child():
    m = _member("bob", ["Kids"])
    assert bot.get_user_role_type(m) == 'child'


def test_role_type_neither():
    m = _member("eve", ["RandomRole"])
    assert bot.get_user_role_type(m) == 'neither'


@pytest.mark.asyncio
async def test_alert_fires_on_one_adult_one_child():
    adult = _member("alice", ["Dads"])
    child = _member("kid", ["Kids"])
    channel = MagicMock()
    channel.members = [adult, child]
    channel.name = 'vc-1'

    mod_channel = MagicMock()
    mod_channel.send = AsyncMock()
    channel.guild.text_channels = [mod_channel]
    mod_channel.name = 'moderators_only'

    await bot.check_voice_channel_safety(channel)

    # Both should have been muted
    adult.edit.assert_awaited_with(mute=True)
    child.edit.assert_awaited_with(mute=True)
    # Mods notified
    mod_channel.send.assert_awaited_once()
    sent = mod_channel.send.call_args[0][0]
    assert "ALERT" in sent


@pytest.mark.asyncio
async def test_no_alert_two_adults_one_child():
    adults = [_member("a", ["Dads"]), _member("b", ["GrownUps"])]
    child = _member("kid", ["Kids"])
    channel = MagicMock()
    channel.members = adults + [child]
    channel.name = 'vc-1'
    mod_channel = MagicMock()
    mod_channel.send = AsyncMock()
    channel.guild.text_channels = [mod_channel]
    mod_channel.name = 'moderators_only'

    await bot.check_voice_channel_safety(channel)

    mod_channel.send.assert_not_called()
    for a in adults:
        a.edit.assert_not_called()
    child.edit.assert_not_called()


@pytest.mark.asyncio
async def test_no_alert_two_children():
    children = [_member("k1", ["Kids"]), _member("k2", ["Bambinos"])]
    channel = MagicMock()
    channel.members = children
    channel.name = 'vc-1'
    mod_channel = MagicMock()
    mod_channel.send = AsyncMock()
    channel.guild.text_channels = [mod_channel]
    mod_channel.name = 'moderators_only'

    await bot.check_voice_channel_safety(channel)
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_bots_ignored_in_count():
    adult = _member("alice", ["Dads"])
    child = _member("kid", ["Kids"])
    bot_member = _member("bot", ["Dads"], is_bot=True)
    channel = MagicMock()
    channel.members = [adult, child, bot_member]
    channel.name = 'vc-1'
    mod_channel = MagicMock()
    mod_channel.send = AsyncMock()
    channel.guild.text_channels = [mod_channel]
    mod_channel.name = 'moderators_only'

    await bot.check_voice_channel_safety(channel)
    # Bot in channel doesn't change the 1-adult-1-child count
    mod_channel.send.assert_awaited_once()
    bot_member.edit.assert_not_called()
