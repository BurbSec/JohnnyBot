"""clone_category_permissions skip-rule coverage.

These tests target the hierarchy/admin/managed/dangerous-perm filters —
the most error-prone part of the clone logic. They are what makes a
future consolidation of clone_category/clone_channel safe.
"""
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import commands


def _perms(**overrides):
    p = MagicMock(spec=discord.Permissions)
    # Default all dangerous perms to False
    for k in ('administrator', 'ban_members', 'kick_members',
              'manage_roles', 'manage_guild', 'manage_channels',
              'manage_messages', 'moderate_members'):
        setattr(p, k, overrides.get(k, False))
    return p


def _role(name, position=1, managed=False, **perms):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.position = position
    r.managed = managed
    r.permissions = _perms(**perms)
    # Make role comparison work like discord.Role
    r.__ge__ = lambda self, other: self.position >= other.position
    r.__lt__ = lambda self, other: self.position < other.position
    return r


def _category(name, overwrites, guild):
    c = MagicMock(spec=discord.CategoryChannel)
    c.name = name
    c.guild = guild
    c.overwrites = overwrites
    c.set_permissions = AsyncMock()
    return c


def _interaction(guild):
    inter = MagicMock()
    inter.guild = guild
    inter.user = MagicMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _guild_with_bot(bot_role_pos=10):
    guild = MagicMock()
    guild.id = 1
    bot_member = MagicMock()
    bot_member.top_role = _role("bot_role", position=bot_role_pos)
    bot_member.guild_permissions = MagicMock()
    bot_member.guild_permissions.manage_roles = True
    guild.me = bot_member
    return guild


def _overwrite():
    return MagicMock(spec=discord.PermissionOverwrite)


@pytest.mark.asyncio
async def test_skips_administrator_role():
    guild = _guild_with_bot()
    admin = _role("admins", position=5, administrator=True)
    src = _category("src", {admin: _overwrite()}, guild)
    dst = _category("dst", {}, guild)

    inter = _interaction(guild)
    await commands.clone_category_permissions(inter, src, dst)

    # Admin role's overwrite should never be applied
    for call in dst.set_permissions.await_args_list:
        assert call.args[0] is not admin


@pytest.mark.asyncio
async def test_skips_managed_role():
    guild = _guild_with_bot()
    mgd = _role("booster", position=5, managed=True)
    src = _category("src", {mgd: _overwrite()}, guild)
    dst = _category("dst", {}, guild)

    inter = _interaction(guild)
    await commands.clone_category_permissions(inter, src, dst)

    for call in dst.set_permissions.await_args_list:
        assert call.args[0] is not mgd


@pytest.mark.asyncio
async def test_skips_role_above_bot_hierarchy():
    guild = _guild_with_bot(bot_role_pos=10)
    high = _role("above_bot", position=20)
    src = _category("src", {high: _overwrite()}, guild)
    dst = _category("dst", {}, guild)

    inter = _interaction(guild)
    await commands.clone_category_permissions(inter, src, dst)

    for call in dst.set_permissions.await_args_list:
        assert call.args[0] is not high


@pytest.mark.asyncio
async def test_skips_role_with_dangerous_perms():
    guild = _guild_with_bot()
    danger = _role("mods", position=5, ban_members=True)
    src = _category("src", {danger: _overwrite()}, guild)
    dst = _category("dst", {}, guild)

    inter = _interaction(guild)
    await commands.clone_category_permissions(inter, src, dst)

    for call in dst.set_permissions.await_args_list:
        assert call.args[0] is not danger


@pytest.mark.asyncio
async def test_copies_safe_role():
    guild = _guild_with_bot()
    safe = _role("members", position=5)
    ow = _overwrite()
    src = _category("src", {safe: ow}, guild)
    dst = _category("dst", {}, guild)

    inter = _interaction(guild)
    await commands.clone_category_permissions(inter, src, dst)

    # Should have been copied to dst
    copied = [c for c in dst.set_permissions.await_args_list
              if c.args[0] is safe and c.kwargs.get('overwrite') is ow]
    assert copied, "Safe role overwrite should be copied to dst"


@pytest.mark.asyncio
async def test_rejects_cross_guild():
    g1 = _guild_with_bot()
    g2 = MagicMock(); g2.id = 99
    src = _category("src", {}, g1)
    dst = _category("dst", {}, g2)

    inter = _interaction(g1)
    await commands.clone_category_permissions(inter, src, dst)

    msg = inter.followup.send.call_args.args[0]
    assert "same server" in msg
