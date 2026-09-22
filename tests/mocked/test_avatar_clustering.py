"""Avatar clustering: grouping bulk-created raid accounts by image."""
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import bot
import config


def _member(name, avatar_bytes=None, avatar_key=None, read_error=None):
    """A member with (or without) a custom avatar.

    avatar_bytes=None means no custom avatar at all — Member.avatar is
    None, which is what a default-avatar account looks like.
    """
    m = MagicMock()
    m.name = name
    m.__str__ = lambda self: name

    if avatar_bytes is None and read_error is None:
        m.avatar = None
        return m

    asset = MagicMock()
    asset.key = avatar_key or f'key-{name}'
    asset.read = AsyncMock(
        side_effect=read_error) if read_error else AsyncMock(
            return_value=avatar_bytes)
    m.avatar = asset
    return m


@pytest.fixture(autouse=True)
def _reset_cache():
    bot._avatar_hash_cache.clear()
    yield
    bot._avatar_hash_cache.clear()


# ── clustering ──

@pytest.mark.asyncio
async def test_identical_images_cluster_together():
    same = b'\x89PNG identical-bytes'
    members = [
        _member('raider1', same, avatar_key='k1'),
        _member('raider2', same, avatar_key='k2'),
        _member('raider3', same, avatar_key='k3'),
    ]

    result = await bot.cluster_members_by_avatar(members)
    clusters, no_avatar = result['clusters'], result['no_avatar']
    scanned, skipped = result['scanned'], result['skipped']

    assert skipped is False
    assert scanned == 3
    assert no_avatar == []
    assert len(clusters) == 1
    assert {m.name for m in clusters[0]} == {'raider1', 'raider2', 'raider3'}


@pytest.mark.asyncio
async def test_distinct_images_do_not_cluster():
    members = [
        _member('alice', b'image-a', avatar_key='ka'),
        _member('bob', b'image-b', avatar_key='kb'),
    ]

    result = await bot.cluster_members_by_avatar(members)
    clusters, scanned = result['clusters'], result['scanned']

    assert clusters == []
    assert scanned == 2


@pytest.mark.asyncio
async def test_mixed_batch_reports_only_the_shared_group():
    same = b'bulk-avatar'
    members = [
        _member('raider1', same, avatar_key='k1'),
        _member('raider2', same, avatar_key='k2'),
        _member('real_person', b'unique-selfie', avatar_key='k3'),
    ]

    result = await bot.cluster_members_by_avatar(members)
    clusters, scanned = result['clusters'], result['scanned']

    assert scanned == 3
    assert len(clusters) == 1
    assert {m.name for m in clusters[0]} == {'raider1', 'raider2'}


@pytest.mark.asyncio
async def test_no_avatar_members_are_counted_never_clustered():
    """They share a Discord default derived from their user id, so
    grouping them would be meaningless — but a lot of them is its own
    raid signal, hence the separate count."""
    members = [_member('a'), _member('b'), _member('c')]

    result = await bot.cluster_members_by_avatar(members)

    assert result['clusters'] == []
    assert len(result['no_avatar']) == 3
    assert result['scanned'] == 3


@pytest.mark.asyncio
async def test_larger_cluster_is_listed_first():
    members = [
        _member('x1', b'img-x', avatar_key='k1'),
        _member('x2', b'img-x', avatar_key='k2'),
        _member('y1', b'img-y', avatar_key='k3'),
        _member('y2', b'img-y', avatar_key='k4'),
        _member('y3', b'img-y', avatar_key='k5'),
    ]

    clusters = (await bot.cluster_members_by_avatar(members))['clusters']

    assert [len(c) for c in clusters] == [3, 2]


# ── cost control ──

@pytest.mark.asyncio
async def test_repeated_avatar_key_is_downloaded_once():
    shared_key = 'same-cdn-object'
    first = _member('a', b'img', avatar_key=shared_key)
    second = _member('b', b'img', avatar_key=shared_key)

    await bot.cluster_members_by_avatar([first])
    await bot.cluster_members_by_avatar([second])

    first.avatar.read.assert_awaited_once()
    second.avatar.read.assert_not_called()


@pytest.mark.asyncio
async def test_expired_cache_entry_is_refetched(monkeypatch):
    monkeypatch.setattr(config, 'RAID_AVATAR_CACHE_TTL', 0)
    member = _member('a', b'img', avatar_key='k')

    await bot.cluster_members_by_avatar([member])
    await bot.cluster_members_by_avatar([member])

    assert member.avatar.read.await_count == 2


@pytest.mark.asyncio
async def test_scan_is_skipped_past_the_limit(monkeypatch):
    monkeypatch.setattr(config, 'RAID_AVATAR_SCAN_LIMIT', 2)
    members = [_member(f'm{i}', b'img', avatar_key=f'k{i}') for i in range(3)]

    result = await bot.cluster_members_by_avatar(members)
    clusters, no_avatar = result['clusters'], result['no_avatar']
    scanned, skipped = result['scanned'], result['skipped']

    assert skipped is True
    assert (clusters, no_avatar, scanned) == ([], [], 0)
    for m in members:
        m.avatar.read.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_clustering_downloads_nothing(monkeypatch):
    monkeypatch.setattr(config, 'RAID_AVATAR_CLUSTERING_ENABLED', False)
    member = _member('a', b'img', avatar_key='k')

    result = await bot.cluster_members_by_avatar([member])
    clusters, no_avatar = result['clusters'], result['no_avatar']
    scanned, skipped = result['scanned'], result['skipped']

    assert (clusters, no_avatar, scanned, skipped) == ([], [], 0, False)
    member.avatar.read.assert_not_called()


# ── failure handling ──

@pytest.mark.asyncio
async def test_one_unreadable_avatar_does_not_fail_the_batch():
    same = b'bulk-avatar'
    members = [
        _member('raider1', same, avatar_key='k1'),
        _member('broken', read_error=discord.HTTPException(
            MagicMock(status=500, reason='err'), 'boom'), avatar_key='k2'),
        _member('raider2', same, avatar_key='k3'),
    ]

    result = await bot.cluster_members_by_avatar(members)

    assert result['scanned'] == 3
    assert len(result['clusters']) == 1
    assert {m.name for m in result['clusters'][0]} == {'raider1', 'raider2'}
    # Not clustered, and NOT reported as "has no avatar" — they do have
    # one, it just couldn't be downloaded.
    assert result['no_avatar'] == []
    assert result['unreadable'] == 1


@pytest.mark.asyncio
async def test_a_member_raising_outright_is_skipped():
    """asyncio.gather(return_exceptions=True) captures it, so the rest of
    the batch still clusters."""
    exploding = MagicMock()
    type(exploding).avatar = property(
        lambda self: (_ for _ in ()).throw(RuntimeError('boom')))
    members = [
        exploding,
        _member('raider1', b'img', avatar_key='k1'),
        _member('raider2', b'img', avatar_key='k2'),
    ]

    result = await bot.cluster_members_by_avatar(members)

    assert len(result['clusters']) == 1
    assert result['scanned'] == 2  # the exploding one never counted


@pytest.mark.asyncio
async def test_report_never_raises(monkeypatch):
    """The raid alert calls this — a clustering failure must not cost
    moderators their alert. Forces the failure at the clustering call
    itself, so the outer guard is what's actually under test."""
    async def _boom(_members):
        raise RuntimeError('clustering exploded')

    monkeypatch.setattr(bot, 'cluster_members_by_avatar', _boom)

    assert await bot._avatar_cluster_report([_member('a')]) == ''


# ── report formatting ──

def _result(clusters=(), no_avatar=(), unreadable=0, scanned=0, skipped=False):
    return {'clusters': list(clusters), 'no_avatar': list(no_avatar),
            'unreadable': unreadable, 'scanned': scanned, 'skipped': skipped}


def test_format_highlights_a_shared_avatar():
    group = [_member('a'), _member('b'), _member('c')]
    text = bot.format_avatar_clusters(_result([group], scanned=4))

    assert '3 of 4' in text
    assert 'Group 1 (3)' in text


def test_format_reports_missing_avatars():
    text = bot.format_avatar_clusters(
        _result(no_avatar=[_member('a'), _member('b')], scanned=5))
    assert '2 of 5' in text
    assert 'no avatar set' in text


def test_format_says_so_when_everything_is_distinct():
    text = bot.format_avatar_clusters(_result(scanned=6))
    assert 'All 6 avatars are distinct' in text


def test_format_excludes_unreadable_from_the_checked_count():
    """Saying 'all 6 distinct' when one was never downloaded would be a
    false statement to a moderator deciding whether to kick."""
    text = bot.format_avatar_clusters(_result(unreadable=2, scanned=6))

    assert 'All 4 avatars are distinct' in text
    assert '2 avatar(s) could not be checked' in text


def test_format_explains_a_skipped_scan():
    text = bot.format_avatar_clusters(_result(skipped=True))
    assert 'skipped' in text


def test_format_is_empty_when_nothing_was_scanned():
    assert bot.format_avatar_clusters(_result()) == ''


def test_format_stays_within_discords_message_limit():
    """A big raid must not turn the alert into an HTTPException."""
    clusters = [[_member(f'raider{i}-{j}') for j in range(40)]
                for i in range(20)]
    text = bot.format_avatar_clusters(
        _result(clusters, no_avatar=[_member('x')], scanned=800))

    assert len(text) < 2000


def test_format_caps_the_number_of_groups_listed():
    clusters = [[_member(f'a{i}'), _member(f'b{i}')] for i in range(9)]
    text = bot.format_avatar_clusters(_result(clusters, scanned=18))

    assert 'Group 5 (2)' in text
    assert 'Group 6 (2)' not in text
    assert 'and 4 more group(s)' in text
