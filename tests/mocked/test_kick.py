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


def _interaction(guild, user_id=999, user_top_role_pos=50):
    inter = MagicMock()
    inter.guild = guild
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.top_role = _role("invoker_role", position=user_top_role_pos)
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _guild_with_members(members, bot_top_pos=10, has_kick_perm=True,
                        owner_id=1):
    guild = MagicMock()
    by_id = {m.id: m for m in members}
    guild.get_member.side_effect = lambda uid: by_id.get(uid)
    guild.members = members
    guild.owner_id = owner_id

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


# ---------------------------------------------------------------------
# Invoker role hierarchy — a moderator must not be able to kick someone
# ranked at or above themselves just because the bot outranks them.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kick_skips_member_outranking_invoker():
    """Bot is above the target, but the invoker is not."""
    boss = _member("boss", top_role_pos=8, member_id=200)
    guild, _ = _guild_with_members([boss], bot_top_pos=10)
    inter = _interaction(guild, user_top_role_pos=3)

    await commands.kick_members(inter, "<@200>")

    boss.kick.assert_not_called()
    assert "outranks you" in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_kick_skips_member_of_equal_rank():
    peer = _member("peer", top_role_pos=5, member_id=201)
    guild, _ = _guild_with_members([peer], bot_top_pos=10)
    inter = _interaction(guild, user_top_role_pos=5)

    await commands.kick_members(inter, "<@201>")

    peer.kick.assert_not_called()
    assert "outranks you" in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_kick_allows_member_below_invoker():
    junior = _member("junior", top_role_pos=2, member_id=202)
    guild, _ = _guild_with_members([junior], bot_top_pos=10)
    inter = _interaction(guild, user_top_role_pos=7)

    await commands.kick_members(inter, "<@202>")

    junior.kick.assert_awaited_once()


@pytest.mark.asyncio
async def test_kick_never_targets_the_guild_owner():
    owner = _member("owner", top_role_pos=2, member_id=777)
    guild, _ = _guild_with_members([owner], bot_top_pos=10, owner_id=777)
    inter = _interaction(guild, user_top_role_pos=9)

    await commands.kick_members(inter, "<@777>")

    owner.kick.assert_not_called()
    assert "outranks you" in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_guild_owner_may_kick_anyone_below_the_bot():
    target = _member("target", top_role_pos=9, member_id=203)
    guild, _ = _guild_with_members([target], bot_top_pos=10, owner_id=999)
    inter = _interaction(guild, user_id=999, user_top_role_pos=1)

    await commands.kick_members(inter, "<@203>")

    target.kick.assert_awaited_once()


@pytest.mark.asyncio
async def test_kick_role_skips_member_outranking_invoker():
    boss = _member("boss", top_role_pos=8, member_id=300)
    guild, _ = _guild_with_members([boss], bot_top_pos=10)
    inter = _interaction(guild, user_top_role_pos=3)

    role = _role("Members", position=1)
    boss.roles = [role]

    await commands.kick_role(inter, role)

    boss.kick.assert_not_called()
    assert "outranks you" in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_kick_role_does_not_kick_the_invoker():
    """kick_members already skipped self; kick_role used to remove the
    moderator who ran it."""
    me = _member("me", top_role_pos=5, member_id=999)
    other = _member("other", top_role_pos=2, member_id=301)
    guild, _ = _guild_with_members([me, other], bot_top_pos=10)
    inter = _interaction(guild, user_id=999, user_top_role_pos=5)

    role = _role("Members", position=1)
    me.roles = [role]
    other.roles = [role]

    await commands.kick_role(inter, role)

    me.kick.assert_not_called()
    other.kick.assert_awaited_once()
