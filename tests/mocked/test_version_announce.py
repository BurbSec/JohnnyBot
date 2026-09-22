"""Version-change announcements on startup."""
import json
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

import pytest

import bot
import config


OLD_SHA = 'a' * 40
NEW_SHA = 'b' * 40


def _guild(guild_id=1, with_mod_channel=True):
    g = MagicMock()
    g.id = guild_id
    g.name = f'guild-{guild_id}'
    channels = []
    mod_channel = None
    if with_mod_channel:
        mod_channel = MagicMock()
        mod_channel.name = 'moderators_only'
        mod_channel.send = AsyncMock()
        channels.append(mod_channel)
    g.text_channels = channels
    return g, mod_channel


def _fake_git(head=NEW_SHA, tag='', notes='', ancestor=True):
    """Stand in for _run_cmd, dispatching on the git subcommand."""
    async def _run(*args):
        if args[:2] == ('git', 'rev-parse'):
            return (0, head, '') if head else (1, '', 'not a repo')
        if args[:2] == ('git', 'tag'):
            return 0, tag, ''
        if args[:2] == ('git', 'merge-base'):
            return (0 if ancestor else 1), '', ''
        if args[:2] == ('git', 'for-each-ref'):
            return (0, notes, '') if notes else (1, '', '')
        return 1, '', 'unexpected'
    return _run


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every test off the real repo and the real state file."""
    monkeypatch.setattr(bot, 'VERSION_STATE_FILE',
                        str(tmp_path / 'version_state.json'))
    monkeypatch.setattr(bot, '_run_cmd', _fake_git())
    monkeypatch.setattr(type(bot.bot), 'guilds', property(lambda self: []))
    yield


def _seed_state(sha=OLD_SHA, label='v1.0.0'):
    with open(bot.VERSION_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'commit': sha, 'label': label}, f)


def _stored():
    with open(bot.VERSION_STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _use_guilds(monkeypatch, guilds):
    monkeypatch.setattr(type(bot.bot), 'guilds',
                        property(lambda self: guilds))


# ── when it stays silent ──

@pytest.mark.asyncio
async def test_first_run_records_baseline_without_announcing(monkeypatch):
    """Otherwise every existing install posts a bogus 'updated' notice
    the first time this ships."""
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])

    await bot.announce_version_change()

    mod_channel.send.assert_not_called()
    assert _stored()['commit'] == NEW_SHA


@pytest.mark.asyncio
async def test_unchanged_commit_is_silent(monkeypatch):
    """An ordinary restart, reconnect or crash-loop must not announce."""
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state(sha=NEW_SHA, label='v1.1.0')

    await bot.announce_version_change()

    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_does_nothing(monkeypatch):
    monkeypatch.setattr(config, 'VERSION_ANNOUNCE_ENABLED', False)
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_missing_git_is_silent_not_fatal(monkeypatch):
    """Tarball and container installs have no checkout; update checking
    already assumes one, so this opts out rather than erroring."""
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(head=''))
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    mod_channel.send.assert_not_called()


# ── when it announces ──

@pytest.mark.asyncio
async def test_changed_commit_announces_both_labels(monkeypatch):
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(tag='v1.1.0'))
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state(label='v1.0.0')

    await bot.announce_version_change()

    mod_channel.send.assert_awaited_once()
    sent = mod_channel.send.call_args[0][0]
    assert 'v1.0.0' in sent and 'v1.1.0' in sent
    assert 'updated' in sent.lower()


@pytest.mark.asyncio
async def test_every_guild_moderators_channel_is_notified(monkeypatch):
    """_get_moderators_channel returns only the first match across all
    guilds, which would leave every other server unaware."""
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(tag='v1.1.0'))
    g1, mod1 = _guild(1)
    g2, mod2 = _guild(2)
    g3, _none = _guild(3, with_mod_channel=False)
    _use_guilds(monkeypatch, [g1, g2, g3])
    _seed_state()

    await bot.announce_version_change()

    mod1.send.assert_awaited_once()
    mod2.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_tag_annotation_becomes_the_body(monkeypatch):
    notes = 'v1.1.0: safety systems\n\nNew:\n- Raid protection'
    monkeypatch.setattr(bot, '_run_cmd',
                        _fake_git(tag='v1.1.0', notes=notes))
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    assert '- Raid protection' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_untagged_commit_still_announces(monkeypatch):
    """Deployed between releases: no tag, no body, but the version
    change itself must not go unreported."""
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(tag=''))
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    mod_channel.send.assert_awaited_once()
    assert bot._short_sha(NEW_SHA) in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_rollback_is_labelled_differently(monkeypatch):
    """A surprise checkout of an older tag must not read as an upgrade."""
    monkeypatch.setattr(bot, '_run_cmd',
                        _fake_git(tag='v1.0.0', ancestor=False))
    g, mod_channel = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state(label='v1.1.0')

    await bot.announce_version_change()

    sent = mod_channel.send.call_args[0][0]
    assert 'rollback' in sent.lower() or 'diverged' in sent.lower()


@pytest.mark.asyncio
async def test_state_is_updated_after_announcing(monkeypatch):
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(tag='v1.1.0'))
    g, _mod = _guild()
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    stored = _stored()
    assert stored['commit'] == NEW_SHA
    assert stored['label'] == 'v1.1.0'
    # Parses as a real timestamp — comparing it to itself would pass on
    # None or an empty string.
    assert datetime.fromisoformat(stored['recorded_at']).tzinfo is not None


@pytest.mark.asyncio
async def test_send_failure_does_not_prevent_recording(monkeypatch):
    """Otherwise a transient Discord error would re-announce the same
    version on every subsequent restart."""
    monkeypatch.setattr(bot, '_run_cmd', _fake_git(tag='v1.1.0'))
    g, mod_channel = _guild()
    import discord
    mod_channel.send = AsyncMock(side_effect=discord.HTTPException(
        MagicMock(status=500, reason='err'), 'boom'))
    _use_guilds(monkeypatch, [g])
    _seed_state()

    await bot.announce_version_change()

    assert _stored()['commit'] == NEW_SHA


# ── release-note handling ──

def test_ssh_signature_is_stripped():
    """This repo signs tags with SSH — without stripping, a wall of
    base64 gets posted to the moderators channel."""
    notes = bot._strip_signature(
        'v1.0.0: the real summary\n'
        '-----BEGIN SSH SIGNATURE-----\n'
        'U1NIU0lHAAAAAQAAARcAAAAHc3NoLXJzYQ\n'
        '-----END SSH SIGNATURE-----')

    assert notes == 'v1.0.0: the real summary'


def test_pgp_signature_is_stripped():
    notes = bot._strip_signature(
        'v2.0.0: notes\n'
        '-----BEGIN PGP SIGNATURE-----\n'
        'iQIzBAABCgAdFiEE\n'
        '-----END PGP SIGNATURE-----')

    assert notes == 'v2.0.0: notes'


def test_unsigned_annotation_is_untouched():
    assert bot._strip_signature('v1.1.0: plain notes') == 'v1.1.0: plain notes'


@pytest.mark.asyncio
async def test_long_release_notes_are_truncated(monkeypatch):
    monkeypatch.setattr(bot, '_run_cmd',
                        _fake_git(tag='v1.1.0', notes='x' * 5000))

    notes = await bot._tag_release_notes('v1.1.0')

    assert len(notes) <= bot._RELEASE_NOTES_BUDGET + 2
    assert notes.endswith('…')


@pytest.mark.asyncio
async def test_untagged_label_asks_git_for_nothing(monkeypatch):
    """A short SHA is not a tag name — don't shell out looking for one."""
    calls = []

    async def _tracking(*args):
        calls.append(args)
        return 1, '', ''

    monkeypatch.setattr(bot, '_run_cmd', _tracking)

    assert await bot._tag_release_notes('a1b2c3d4') == ''
    assert calls == []
