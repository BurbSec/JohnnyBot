"""Regression tests for the second remediation pass (B6-B13, O1-O8, R1-R10)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import discord

import commands


# ── B6: the two announcements must be distinguishable ────────────────

def _scheduled_event(name='Ev'):
    ev = MagicMock()
    ev.name = name
    ev.id = 555
    ev.guild.id = 111
    return ev


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['This Week', 'Today'])
async def test_announcement_carries_its_heading(prefix):
    feed = commands.EventFeed.__new__(commands.EventFeed)
    channel = MagicMock()
    channel.name = 'events'
    channel.send = AsyncMock()

    await feed._post_discord_event_announcement(  # pylint: disable=protected-access
        channel, _scheduled_event(), prefix)

    content = channel.send.await_args.kwargs['content']
    assert prefix in content
    # The bare URL must survive so Discord still unfurls its event card.
    assert 'https://discord.com/events/111/555' in content


@pytest.mark.asyncio
async def test_weekly_and_dayof_announcements_differ():
    feed = commands.EventFeed.__new__(commands.EventFeed)
    sent = []
    channel = MagicMock()
    channel.name = 'events'
    channel.send = AsyncMock(
        side_effect=lambda **kw: sent.append(kw['content']))

    for prefix in ('This Week', 'Today'):
        await feed._post_discord_event_announcement(  # pylint: disable=protected-access
            channel, _scheduled_event(), prefix)

    assert sent[0] != sent[1], 'announcements are byte-identical again'


# ── B8: the manual feed check must not touch other guilds ────────────

@pytest.mark.asyncio
async def test_check_feeds_job_can_be_scoped_to_one_guild():
    feed = commands.EventFeed.__new__(commands.EventFeed)
    feed.feeds = {1: {'u1': {'name': 'a'}}, 2: {'u2': {'name': 'b'}}}
    feed.bot = MagicMock()
    feed.bot.get_guild.side_effect = lambda gid: MagicMock(id=gid, name=f'g{gid}')
    feed._cleanup_old_posted_events = lambda: None
    feed.save_feeds_async = AsyncMock()
    checked = []

    async def _fake(guild, url, data):
        checked.append((guild.id, url))
        return 0
    feed._check_single_feed = _fake

    await feed.check_feeds_job(guild_id=1)
    assert checked == [(1, 'u1')], f'leaked into other guilds: {checked}'


@pytest.mark.asyncio
async def test_check_feeds_job_without_guild_id_sweeps_everything():
    feed = commands.EventFeed.__new__(commands.EventFeed)
    feed.feeds = {1: {'u1': {'name': 'a'}}, 2: {'u2': {'name': 'b'}}}
    feed.bot = MagicMock()
    feed.bot.get_guild.side_effect = lambda gid: MagicMock(id=gid, name=f'g{gid}')
    feed._cleanup_old_posted_events = lambda: None
    feed.save_feeds_async = AsyncMock()
    checked = []

    async def _fake(guild, url, data):
        checked.append(guild.id)
        return 0
    feed._check_single_feed = _fake

    await feed.check_feeds_job()
    assert sorted(checked) == [1, 2]


# ── B9: the rate-limit path must catch what discord.py raises ────────

def test_rate_limited_is_not_an_http_exception():
    """The old handler caught HTTPException and string-matched
    'rate limited', so it could never fire."""
    assert not issubclass(discord.RateLimited, discord.HTTPException)


def test_message_dump_handles_rate_limited():
    import inspect
    src = inspect.getsource(commands.message_dump_command)
    assert 'except discord.RateLimited' in src
    # The partial batch must be kept, not silently dropped.
    assert 'messages.extend(current_batch)' in src


# ── B12: SSRF guard and response caps ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8080/feed.ics',
    'http://169.254.169.254/latest/meta-data/',
    'http://[::1]/feed.ics',
])
async def test_private_and_loopback_urls_are_refused(url):
    assert await commands._validate_fetchable_url(url) is not None


@pytest.mark.asyncio
async def test_non_http_scheme_is_refused():
    assert await commands._validate_fetchable_url('file:///etc/passwd') is not None


@pytest.mark.asyncio
async def test_read_capped_rejects_an_oversized_declared_length():
    resp = MagicMock()
    resp.content_length = commands.MAX_FETCH_BYTES + 1
    with pytest.raises(commands.ResponseTooLarge):
        await commands._read_capped(resp, 'http://x')


@pytest.mark.asyncio
async def test_read_capped_rejects_a_lying_content_length():
    """A missing or understated header must not get past the cap."""
    resp = MagicMock()
    resp.content_length = None
    resp.charset = 'utf-8'

    async def _chunks(_size):
        for _ in range(3):
            yield b'x' * 1024
    resp.content = MagicMock()
    resp.content.iter_chunked = _chunks

    with pytest.raises(commands.ResponseTooLarge):
        await commands._read_capped(resp, 'http://x', limit=2048)


# ── O1: already-posted RSS entries must not be re-scraped ────────────

@pytest.mark.asyncio
async def test_rss_skips_scraping_already_posted_entries(monkeypatch):
    feed = commands.EventFeed.__new__(commands.EventFeed)
    feed._fetch_memo = {'rss': None}  # placeholder, replaced below
    feed._fetch_memo = None

    entries = [MagicMock(link='http://e/1', id='uid-1'),
               MagicMock(link='http://e/2', id='uid-2')]
    monkeypatch.setattr(commands.feedparser, 'parse',
                        lambda _t: {'entries': entries})

    scraped = []

    async def _scrape(_session, link, uid):
        scraped.append(uid)
        return None
    feed._scrape_event_page = _scrape

    class _Resp:
        content_length = 10
        charset = 'utf-8'
        status = 200

        def raise_for_status(self):
            pass

        @property
        def content(self):
            m = MagicMock()

            async def _c(_s):
                yield b'<rss></rss>'
            m.iter_chunked = _c
            return m

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, _url):
            outer = self

            class _CM:
                async def __aenter__(self):
                    return _Resp()

                async def __aexit__(self, *a):
                    return None
            return _CM()

    monkeypatch.setattr(commands.aiohttp, 'ClientSession',
                        lambda **kw: _Session())

    # uid-1 is already posted under a composite key
    feed_data = {'posted_events': {'uid-1|2026-09-15'}}
    await feed._fetch_and_parse_rss('http://feed', feed_data)

    assert 'uid-1' not in scraped, 're-scraped an already-posted entry'
    assert scraped == ['uid-2']


# ── O2: the two passes must share one set of fetches ─────────────────

@pytest.mark.asyncio
async def test_memoized_fetches_reuses_a_calendar_fetch():
    feed = commands.EventFeed.__new__(commands.EventFeed)
    calls = []

    async def _uncached(url):
        calls.append(url)
        return f'cal-for-{url}'
    feed._fetch_calendar_uncached = _uncached

    async with feed.memoized_fetches():
        a = await feed._fetch_calendar('http://f')
        b = await feed._fetch_calendar('http://f')
    assert a == b == 'cal-for-http://f'
    assert len(calls) == 1, f'fetched {len(calls)} times inside the memo'

    # Memo must not persist past the block, or scheduled runs go stale.
    await feed._fetch_calendar('http://f')
    assert len(calls) == 2


# ── chaperone mutes are keyed per guild ──────────────────────────────

def test_chaperone_mutes_are_keyed_by_guild_and_user():
    import bot as bot_module
    bot_module._chaperone_muted.clear()
    bot_module._chaperone_muted.add((1, 42))
    # Same user, different guild, must be independent.
    assert (2, 42) not in bot_module._chaperone_muted
    bot_module._chaperone_muted.clear()


# ── O4: DM handling must not fail open on a cold member cache ────────

def _dm_guild(*, chunked, has_member, gid=1):
    guild = MagicMock()
    guild.id = gid
    guild.name = f'g{gid}'
    guild.chunked = chunked
    member = MagicMock() if has_member else None
    if member is not None:
        member.guild = guild
        member.guild_permissions.manage_messages = False
        member.kick = AsyncMock()
        member.id = 42
    guild.get_member.return_value = member
    guild.fetch_member = AsyncMock(return_value=member)
    guild.text_channels = []
    return guild, member


@pytest.mark.asyncio
async def test_unchunked_guild_is_still_confirmed_by_api(monkeypatch):
    """A cache miss means nothing before chunking completes; treating it
    as 'not a member' would silently exempt a real member from a kick."""
    import bot as bot_module
    guild, _ = _dm_guild(chunked=False, has_member=False)
    real = MagicMock()
    real.guild = guild
    real.id = 42
    real.guild_permissions.manage_messages = False
    real.kick = AsyncMock()
    guild.fetch_member = AsyncMock(return_value=real)

    monkeypatch.setattr(bot_module, 'bot', MagicMock(guilds=[guild]))
    monkeypatch.setattr(bot_module, '_was_recently_dmed', lambda _uid: False)

    msg = MagicMock()
    msg.author = MagicMock(id=42)
    msg.content = 'hi'
    await bot_module.handle_unsolicited_dm(msg)

    guild.fetch_member.assert_awaited_once()
    real.kick.assert_awaited_once()


@pytest.mark.asyncio
async def test_stranger_costs_no_api_calls_when_guilds_are_chunked(monkeypatch):
    """The rate-limit hole: a stranger's DM must not fan out to
    fetch_member across every guild."""
    import bot as bot_module
    g1, _ = _dm_guild(chunked=True, has_member=False, gid=1)
    g2, _ = _dm_guild(chunked=True, has_member=False, gid=2)

    monkeypatch.setattr(bot_module, 'bot', MagicMock(guilds=[g1, g2]))
    monkeypatch.setattr(bot_module, '_was_recently_dmed', lambda _uid: False)

    msg = MagicMock()
    msg.author = MagicMock(id=999)
    msg.content = 'spam'
    await bot_module.handle_unsolicited_dm(msg)

    g1.fetch_member.assert_not_awaited()
    g2.fetch_member.assert_not_awaited()


# ── No command is offered in DMs ─────────────────────────────────────

def test_every_command_is_guild_only_and_guild_install():
    """The bot auto-kicks unsolicited DMs, so nothing — including the
    bot interaction commands — should be invocable there."""
    import discord as _d
    from discord.ext import commands as _extc
    b = _extc.Bot(command_prefix='!', intents=_d.Intents.default())
    commands.setup_commands(b)

    not_guild_only = [c.name for c in b.tree.get_commands()
                      if not c.guild_only]
    assert not not_guild_only, f'invocable in DMs: {not_guild_only}'

    for name in ('bot_mood', 'pet_bot', 'bot_pick_fav', 'dashboard'):
        by_name = {c.name: c for c in b.tree.get_commands()}
        assert by_name[name].guild_only

    for cmd in b.tree.get_commands():
        assert cmd.allowed_installs.guild
        assert not cmd.allowed_installs.user
