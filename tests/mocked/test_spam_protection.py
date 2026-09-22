"""Message spam protection: cross-channel link runs and mention spam."""
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import bot
import config


SCAM = 'free nitro here https://totally-legit.example/claim'


def _guild(guild_id=1):
    g = MagicMock()
    g.id = guild_id
    g.name = f'guild-{guild_id}'
    mod_channel = MagicMock()
    mod_channel.name = 'moderators_only'
    mod_channel.send = AsyncMock()
    g.text_channels = [mod_channel]
    # Dispatch by id rather than one fixed mock, so a test can actually
    # tell whether the deleter reached for the right channel.
    g.channels = {}
    g.get_channel.side_effect = g.channels.get
    return g, mod_channel


def _channel(channel_id, guild=None):
    if guild is not None and channel_id in guild.channels:
        return guild.channels[channel_id]
    ch = MagicMock()
    ch.id = channel_id
    ch.mention = f'<#{channel_id}>'
    partial = MagicMock()
    partial.delete = AsyncMock()
    ch.get_partial_message.return_value = partial
    if guild is not None:
        guild.channels[channel_id] = ch
    return ch


def _deleted_in(guild, channel_id):
    """How many copies the deleter removed from a given channel."""
    ch = guild.channels.get(channel_id)
    if ch is None:
        return 0
    return ch.get_partial_message.return_value.delete.await_count


def _author(guild, user_id=100, is_mod=False, account_age_days=30):
    a = MagicMock()
    a.id = user_id
    a.bot = False
    a.guild_permissions = discord.Permissions(
        manage_messages=True) if is_mod else discord.Permissions.none()
    a.created_at = (datetime.now(timezone.utc)
                    - timedelta(days=account_age_days))
    a.timeout = AsyncMock()
    a.kick = AsyncMock()
    a.__str__ = lambda self: f'user-{user_id}'
    return a


def _message(guild, author, channel_id=1, content=SCAM,
             mentions=(), role_mentions=(), message_id=None):
    m = MagicMock()
    m.guild = guild
    m.author = author
    m.channel = _channel(channel_id, guild)
    m.content = content
    m.mentions = list(mentions)
    m.role_mentions = list(role_mentions)
    m.id = message_id if message_id is not None else channel_id * 1000
    m.delete = AsyncMock()
    return m


@pytest.fixture(autouse=True)
def _reset_state():
    bot._link_posts.clear()
    yield
    bot._link_posts.clear()


# ── cross-channel link spam ──

@pytest.mark.asyncio
async def test_same_link_in_two_channels_is_spam():
    g, mod_channel = _guild()
    author = _author(g)

    first = _message(g, author, channel_id=1)
    assert await bot.check_message_spam(first) is False
    first.delete.assert_not_called()

    second = _message(g, author, channel_id=2)
    assert await bot.check_message_spam(second) is True

    second.delete.assert_awaited_once()
    author.timeout.assert_awaited_once()
    mod_channel.send.assert_awaited_once()
    assert 'SPAM PROTECTION TRIGGERED' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_earlier_copy_is_also_deleted():
    """Deleting only the message that tripped it would leave the first
    post standing in the channel it landed in."""
    g, _mod = _guild()
    author = _author(g)

    await bot.check_message_spam(_message(g, author, channel_id=1))
    await bot.check_message_spam(_message(g, author, channel_id=2))

    assert _deleted_in(g, 1) == 1


@pytest.mark.asyncio
async def test_every_earlier_channel_is_cleaned_up():
    """The realistic run hits several channels — each earlier copy must
    be deleted from the channel it actually landed in."""
    g, _mod = _guild()
    author = _author(g)

    for channel_id in (1, 2, 3):
        await bot.check_message_spam(
            _message(g, author, channel_id=channel_id))

    # The 4th post finds copies in all three earlier channels.
    await bot.check_message_spam(_message(g, author, channel_id=4))

    assert _deleted_in(g, 1) >= 1
    assert _deleted_in(g, 2) >= 1
    assert _deleted_in(g, 3) >= 1


@pytest.mark.asyncio
async def test_same_link_twice_in_one_channel_is_not_spam():
    """Ordinary repetition — the cross-channel spread is the signal."""
    g, mod_channel = _guild()
    author = _author(g)

    await bot.check_message_spam(_message(g, author, channel_id=1))
    result = await bot.check_message_spam(
        _message(g, author, channel_id=1, message_id=1001))

    assert result is False
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_different_content_across_channels_is_not_spam():
    g, mod_channel = _guild()
    author = _author(g)

    await bot.check_message_spam(
        _message(g, author, channel_id=1, content='look https://a.example'))
    result = await bot.check_message_spam(
        _message(g, author, channel_id=2, content='other https://b.example'))

    assert result is False
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_and_case_variations_still_match():
    g, _mod = _guild()
    author = _author(g)

    await bot.check_message_spam(
        _message(g, author, channel_id=1, content=SCAM))
    result = await bot.check_message_spam(
        _message(g, author, channel_id=2, content=f'  {SCAM.upper()}  '))

    assert result is True


@pytest.mark.asyncio
async def test_posts_outside_the_window_do_not_pair_up():
    g, mod_channel = _guild()
    author = _author(g)

    await bot.check_message_spam(_message(g, author, channel_id=1))
    # Age the tracked entry past the 30s window.
    key = (g.id, author.id)
    stale = bot._link_posts[key][0]
    bot._link_posts[key][0] = (stale[0] - 60,) + stale[1:]

    result = await bot.check_message_spam(_message(g, author, channel_id=2))
    assert result is False
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_messages_without_links_are_never_tracked():
    g, _mod = _guild()
    author = _author(g)

    await bot.check_message_spam(
        _message(g, author, channel_id=1, content='hello everyone'))
    await bot.check_message_spam(
        _message(g, author, channel_id=2, content='hello everyone'))

    assert not bot._link_posts


@pytest.mark.asyncio
async def test_new_account_is_kicked_instead_of_timed_out():
    g, mod_channel = _guild()
    author = _author(g, account_age_days=1)  # under the 2-day threshold

    await bot.check_message_spam(_message(g, author, channel_id=1))
    await bot.check_message_spam(_message(g, author, channel_id=2))

    author.kick.assert_awaited_once()
    author.timeout.assert_not_called()
    assert 'Kicked' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_established_account_is_timed_out_not_kicked():
    g, mod_channel = _guild()
    author = _author(g, account_age_days=90)

    await bot.check_message_spam(_message(g, author, channel_id=1))
    await bot.check_message_spam(_message(g, author, channel_id=2))

    author.timeout.assert_awaited_once()
    author.kick.assert_not_called()
    assert 'Timed out' in mod_channel.send.call_args[0][0]


# ── mention spam ──

@pytest.mark.asyncio
async def test_three_mentions_is_spam():
    g, mod_channel = _guild()
    author = _author(g)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    assert await bot.check_message_spam(msg) is True

    msg.delete.assert_awaited_once()
    author.timeout.assert_awaited_once()
    assert 'pinged' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_two_mentions_is_fine():
    g, mod_channel = _guild()
    author = _author(g)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock()])

    assert await bot.check_message_spam(msg) is False
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_role_mentions_count_toward_the_threshold():
    g, _mod = _guild()
    author = _author(g)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock()],
                   role_mentions=[MagicMock()])

    assert await bot.check_message_spam(msg) is True


@pytest.mark.asyncio
async def test_link_is_tracked_even_when_mentions_fire_first():
    """A spam run routinely pairs a link with mass pings. If the mention
    response short-circuited before tracking, the cross-post would never
    accumulate and a failed timeout would let the run continue unseen."""
    g, _mod = _guild()
    author = _author(g)
    author.timeout = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'no rank'))
    pings = [MagicMock(), MagicMock(), MagicMock()]

    first = _message(g, author, channel_id=1, content=SCAM, mentions=pings)
    assert await bot.check_message_spam(first) is True

    # Same link, second channel: now recognised as a cross-post run and
    # escalated, rather than handled as another isolated mention ping.
    second = _message(g, author, channel_id=2, content=SCAM, mentions=pings)
    assert await bot.check_message_spam(second) is True
    assert _deleted_in(g, 1) == 1


@pytest.mark.asyncio
async def test_mention_spam_kicks_a_new_account():
    """A brand-new account mass-pinging is the same raid follow-up as
    link spam, so it gets the same escalation."""
    g, _mod = _guild()
    author = _author(g, account_age_days=0)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    await bot.check_message_spam(msg)

    author.kick.assert_awaited_once()
    author.timeout.assert_not_called()


@pytest.mark.asyncio
async def test_mention_spam_times_out_an_established_account():
    g, _mod = _guild()
    author = _author(g, account_age_days=90)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    await bot.check_message_spam(msg)

    author.timeout.assert_awaited_once()
    author.kick.assert_not_called()


# ── exemptions and failure modes ──

@pytest.mark.asyncio
async def test_moderators_are_exempt():
    g, mod_channel = _guild()
    author = _author(g, is_mod=True)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    assert await bot.check_message_spam(msg) is False
    msg.delete.assert_not_called()
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_protection_skips_everything(monkeypatch):
    monkeypatch.setattr(config, 'SPAM_PROTECTION_ENABLED', False)
    g, mod_channel = _guild()
    author = _author(g)
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    assert await bot.check_message_spam(msg) is False
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_forbidden_timeout_is_reported_not_raised():
    g, mod_channel = _guild()
    author = _author(g)
    author.timeout = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'no rank'))
    msg = _message(g, author, content='hey',
                   mentions=[MagicMock(), MagicMock(), MagicMock()])

    await bot.check_message_spam(msg)

    assert 'Could not time them out' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_stale_keys_are_swept_once_the_map_grows():
    g, _mod = _guild()
    author = _author(g)
    now = datetime.now(timezone.utc).timestamp()
    # Stale entries for users who posted a link once and never returned.
    for uid in range(bot._LINK_POSTS_SWEEP_AT + 1):
        bot._link_posts[(g.id, uid)] = deque([(now - 600, 'digest', 1, 1)])
    # A recent poster seeded before the sweep must survive it — asserting
    # only the total would pass even if the sweep dropped live entries.
    live_key = (g.id, 55555)
    bot._link_posts[live_key] = deque([(now, 'digest', 1, 1)])

    await bot.check_message_spam(_message(g, author, channel_id=1))

    assert live_key in bot._link_posts
    # Everything stale is gone: the live key plus the message just posted.
    assert len(bot._link_posts) == 2
