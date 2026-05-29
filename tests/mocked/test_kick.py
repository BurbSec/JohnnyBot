"""kick_members logic — happy path + skip rules."""
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import commands


def _role(name, position=1):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.position = position
    r.__ge__ = lambda self, other: self.position >= other.position
    r.__lt__ = lambda self, other: self.position < other.position
    return r


def _member(name, top_role_pos=1, member_id=100, is_bot=False):
    m = MagicMock()
    m.display_name = name
    m.name = name
    m.id = member_id
    m.bot = is_bot
    m.top_role = _role(f"{name}_role", position=top_role_pos)
    m.kick = AsyncMock()
    return m


def _interaction(guild, user_id=999):
    inter = MagicMock()
    inter.guild = guild
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _guild_with_members(members, bot_top_pos=10, has_kick_perm=True):
    guild = MagicMock()
    by_id = {m.id: m for m in members}
    guild.get_member.side_effect = lambda uid: by_id.get(uid)
    guild.members = members

    bot_self = _member("bot", top_role_pos=bot_top_pos, member_id=1, is_bot=True)
    guild.me = bot_self
    guild.me.guild_permissions.kick_members = has_kick_perm
    return guild, bot_self


@pytest.mark.asyncio
async def test_kick_members_happy_path():
    target = _member("target", top_role_pos=5, member_id=100)
    guild, _ = _guild_with_members([target])
    inter = _interaction(guild)

    await commands.kick_members(inter, "<@100>", reason="bye")

    target.kick.assert_awaited_once()
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "Successfully kicked" in msg or "kicked 1" in msg.lower()


@pytest.mark.asyncio
async def test_kick_skips_higher_role():
    boss = _member("boss", top_role_pos=99, member_id=200)
    guild, _ = _guild_with_members([boss], bot_top_pos=10)
    inter = _interaction(guild)

    await commands.kick_members(inter, "<@200>")

    boss.kick.assert_not_called()
    msg = inter.followup.send.call_args.args[0]
    assert "higher role" in msg


@pytest.mark.asyncio
async def test_kick_skips_self():
    me = _member("me", top_role_pos=5, member_id=999)
    guild, _ = _guild_with_members([me])
    inter = _interaction(guild, user_id=999)

    await commands.kick_members(inter, "<@999>")

    me.kick.assert_not_called()
    msg = inter.followup.send.call_args.args[0]
    assert "yourself" in msg


@pytest.mark.asyncio
async def test_kick_no_permission():
    target = _member("target", top_role_pos=5, member_id=100)
    guild, _ = _guild_with_members([target], has_kick_perm=False)
    inter = _interaction(guild)

    await commands.kick_members(inter, "<@100>")

    target.kick.assert_not_called()
    msg = inter.followup.send.call_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_kick_unknown_user_reported():
    guild, _ = _guild_with_members([])
    inter = _interaction(guild)

    await commands.kick_members(inter, "<@404>")

    msg = inter.followup.send.call_args.args[0]
    assert "No valid members" in msg
