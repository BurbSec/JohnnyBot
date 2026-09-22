"""Raid protection: join-burst detection and lockdown/lift logic."""
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import bot
import commands
import config


def _guild(guild_id=1, invites_paused_until=None):
    g = MagicMock()
    g.id = guild_id
    g.name = f'guild-{guild_id}'
    g.invites_paused_until = invites_paused_until
    g.edit = AsyncMock()
    mod_channel = MagicMock()
    mod_channel.name = 'moderators_only'
    mod_channel.send = AsyncMock()
    g.text_channels = [mod_channel]
    return g, mod_channel


def _member(guild):
    m = MagicMock()
    m.guild = guild
    m.bot = False
    return m


@pytest.fixture(autouse=True)
def _reset_join_times():
    bot._raid_join_times.clear()
    yield
    bot._raid_join_times.clear()


def test_lockdown_not_active_when_no_pause():
    g, _ = _guild()
    assert bot._raid_lockdown_active(g) is False


def test_lockdown_active_with_future_pause():
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    g, _ = _guild(invites_paused_until=until)
    assert bot._raid_lockdown_active(g) is True


def test_lockdown_inactive_with_expired_pause():
    until = datetime.now(timezone.utc) - timedelta(minutes=5)
    g, _ = _guild(invites_paused_until=until)
    assert bot._raid_lockdown_active(g) is False


@pytest.mark.asyncio
async def test_burst_below_threshold_does_not_lock_down():
    g, mod_channel = _guild()
    member = _member(g)
    for _ in range(5):  # default threshold is 6
        await bot.on_member_join(member)
    g.edit.assert_not_called()
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_burst_past_threshold_locks_down_and_alerts():
    g, mod_channel = _guild()
    member = _member(g)
    for _ in range(6):
        await bot.on_member_join(member)

    g.edit.assert_awaited_once()
    kwargs = g.edit.call_args.kwargs
    assert kwargs['invites_disabled_until'] is not None
    assert kwargs['dms_disabled_until'] is not None
    mod_channel.send.assert_awaited_once()
    assert 'RAID PROTECTION TRIGGERED' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_no_second_alert_while_already_locked_down():
    g, mod_channel = _guild(
        invites_paused_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    member = _member(g)
    for _ in range(6):
        await bot.on_member_join(member)

    g.edit.assert_not_called()
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_old_joins_fall_out_of_the_window():
    g, mod_channel = _guild()
    member = _member(g)
    now = datetime.now(timezone.utc).timestamp()
    # 5 joins well outside the 30s window, then 5 fresh ones — if the
    # stale entries weren't purged this would hit the threshold of 6
    # and fire; asserting the exact surviving count also fails if the
    # purge loop is silently broken (e.g. deletes nothing).
    bot._raid_join_times[g.id] = deque([now - 100] * 5)
    for _ in range(5):
        await bot.on_member_join(member)
    assert len(bot._raid_join_times[g.id]) == 5
    g.edit.assert_not_called()


@pytest.mark.asyncio
async def test_bots_are_not_exempt_from_detection():
    g, mod_channel = _guild()
    member = _member(g)
    member.bot = True
    for _ in range(6):
        await bot.on_member_join(member)
    g.edit.assert_awaited_once()
    mod_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_protection_skips_detection(monkeypatch):
    monkeypatch.setattr(config, 'RAID_PROTECTION_ENABLED', False)
    g, mod_channel = _guild()
    member = _member(g)
    for _ in range(10):
        await bot.on_member_join(member)
    g.edit.assert_not_called()
    mod_channel.send.assert_not_called()


def _interaction(guild, user='mod#0001'):
    inter = MagicMock()
    inter.guild = guild
    inter.user = user
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_disabling_protection_lifts_active_lockdown():
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    g, _mod_channel = _guild(invites_paused_until=until)
    inter = _interaction(g)

    await commands.raid_protection_command(inter, False)

    g.edit.assert_awaited_once()
    kwargs = g.edit.call_args.kwargs
    assert kwargs['invites_disabled_until'] is None
    assert kwargs['dms_disabled_until'] is None
    msg = inter.followup.send.call_args.args[0]
    assert 'lockdown was also lifted' in msg
    config.RAID_PROTECTION_ENABLED = True  # restore for later tests


@pytest.mark.asyncio
async def test_disabling_protection_without_lockdown_does_not_edit_guild():
    g, _mod_channel = _guild()  # no invites_paused_until
    inter = _interaction(g)

    await commands.raid_protection_command(inter, False)

    g.edit.assert_not_called()
    config.RAID_PROTECTION_ENABLED = True  # restore for later tests


@pytest.mark.asyncio
async def test_missing_manage_guild_permission_falls_back_to_alert_only():
    g, mod_channel = _guild()
    g.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'missing perms'))
    member = _member(g)
    for _ in range(6):
        await bot.on_member_join(member)
    mod_channel.send.assert_awaited_once()
    assert 'RAID DETECTED' in mod_channel.send.call_args[0][0]
