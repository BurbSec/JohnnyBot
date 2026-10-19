"""/media_dump — collection, link lifecycle and the download server."""
import os
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import commands


def _att(att_id, filename, ctype='image/png', size=10, data=b'x' * 10):
    a = MagicMock()
    a.id = att_id
    a.filename = filename
    a.content_type = ctype
    a.size = size
    a.read = AsyncMock(return_value=data)
    return a


def _msg(msg_id, attachments):
    m = MagicMock()
    m.id = msg_id
    m.created_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    m.attachments = attachments
    return m


def _channel(messages, can_read=True):
    ch = MagicMock()
    ch.name = 'pics'
    ch.mention = '#pics'
    ch.permissions_for.return_value.read_message_history = can_read

    def history(**kwargs):
        ch.history_kwargs = kwargs

        async def gen():
            for m in messages:
                yield m
        return gen()
    ch.history = history
    return ch


def _interaction():
    inter = MagicMock()
    inter.guild.id = 42
    inter.user.id = 7
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.user.send = AsyncMock()
    return inter


def _last_text(inter):
    return inter.followup.send.call_args_list[-1].args[0]


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, 'TEMP_DIR', str(tmp_path))
    monkeypatch.setattr(commands.config, 'MEDIA_DUMP_PORT', 0, raising=False)
    monkeypatch.setattr(commands.config, 'MEDIA_DUMP_HOST', '127.0.0.1', raising=False)
    monkeypatch.setattr(commands, '_public_base_url',
                        AsyncMock(return_value='http://example.test:48731'))
    commands._media_links.clear()
    commands._media_dumps_running.clear()
    yield


async def _teardown():
    await commands._stop_media_server()


def test_is_media_attachment():
    assert commands._is_media_attachment(_att(1, 'a.png', 'image/png'))
    assert commands._is_media_attachment(_att(1, 'a.mp4', 'video/mp4'))
    assert not commands._is_media_attachment(_att(1, 'a.pdf', 'application/pdf'))
    assert commands._is_media_attachment(_att(1, 'a.JPG', None))
    assert not commands._is_media_attachment(_att(1, 'a.txt', None))


def test_archive_name_is_path_safe():
    name = commands._media_archive_name(_msg(5, []), _att(9, '../../etc/pa ss.png'))
    assert '/' not in name and ' ' not in name
    assert name.startswith('20250102_030405_5_9_')


async def test_bad_dates_and_reversed_range():
    inter = _interaction()
    await commands.media_dump_command(inter, _channel([]), 'nope', '2025-01-02')
    assert 'Invalid date' in _last_text(inter)
    inter = _interaction()
    await commands.media_dump_command(inter, _channel([]), '2025-01-03', '2025-01-02')
    assert 'must not be before' in _last_text(inter)


async def test_invoker_without_history_access_is_refused():
    inter = _interaction()
    await commands.media_dump_command(
        inter, _channel([], can_read=False), '2025-01-01', '2025-01-02')
    assert "can't read" in _last_text(inter)


async def test_no_media_gives_no_link_and_no_server():
    ch = _channel([_msg(1, [_att(1, 'doc.pdf', 'application/pdf')])])
    inter = _interaction()
    await commands.media_dump_command(inter, ch, '2025-01-01', '2025-01-02')
    assert 'No media found' in _last_text(inter)
    assert commands._media_server_runner is None
    assert not commands._media_links


async def test_end_date_inclusive_and_oldest_first():
    ch = _channel([])
    await commands.media_dump_command(_interaction(), ch, '2025-01-01', '2025-01-02')
    kw = ch.history_kwargs
    assert kw['after'] == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert kw['before'] == datetime(2025, 1, 3, tzinfo=timezone.utc)
    assert kw['oldest_first'] is True


async def test_happy_path_serves_zip_then_shuts_down(monkeypatch):
    ch = _channel([
        _msg(1, [_att(1, 'a.png', data=b'AAA'), _att(2, 'n.pdf', 'application/pdf')]),
        _msg(2, [_att(3, 'huge.mp4', 'video/mp4', size=500 * 1024 * 1024)]),
    ])
    inter = _interaction()
    try:
        await commands.media_dump_command(inter, ch, '2025-01-01', '2025-01-02')
        token = next(iter(commands._media_links))
        dm = inter.user.send.call_args.args[0]
        assert '**Files:** 1' in dm and '1 file(s) over' in dm
        assert f'http://example.test:48731/dl/{token}' in dm
        assert token not in _last_text(inter)  # the link only goes by DM

        entry = commands._media_links[token]
        with zipfile.ZipFile(entry['path']) as z:
            assert [z.read(n) for n in z.namelist()] == [b'AAA']

        async with TestClient(TestServer(commands._build_media_app())) as client:
            resp = await client.get(f'/dl/{token}')
            assert resp.status == 200
            assert 'attachment' in resp.headers['Content-Disposition']
            assert (await client.get('/dl/wrong')).status == 404
            entry['expires'] = 0
            assert (await client.get(f'/dl/{token}')).status == 404
        assert token not in commands._media_links
        assert not os.path.exists(entry['dir'])
    finally:
        await _teardown()


async def test_shutdown_stops_server_and_deletes_archives():
    ch = _channel([_msg(1, [_att(1, 'a.png')])])
    await commands.media_dump_command(_interaction(), ch, '2025-01-01', '2025-01-02')
    entry = next(iter(commands._media_links.values()))
    assert commands._media_server_runner is not None
    await commands._stop_media_server()
    assert commands._media_server_runner is None
    assert not commands._media_links
    assert not os.path.exists(entry['dir'])


async def test_archive_cap_stops_collection(monkeypatch):
    monkeypatch.setattr(commands.config, 'MEDIA_DUMP_MAX_TOTAL_MB', 0, raising=False)
    ch = _channel([_msg(1, [_att(1, 'a.png')])])
    inter = _interaction()
    await commands.media_dump_command(inter, ch, '2025-01-01', '2025-01-02')
    assert 'No media found' in _last_text(inter)
    assert 'archive cap' in _last_text(inter)


async def test_concurrent_dump_refused():
    commands._media_dumps_running.add(42)
    inter = _interaction()
    await commands.media_dump_command(inter, _channel([]), '2025-01-01', '2025-01-02')
    assert 'already running' in _last_text(inter)


async def test_dm_refused_expires_link_and_stops_server():
    import discord
    ch = _channel([_msg(1, [_att(1, 'a.png')])])
    inter = _interaction()
    inter.user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), 'no'))
    try:
        await commands.media_dump_command(inter, ch, '2025-01-01', '2025-01-02')
        assert 'DM' in _last_text(inter)
        assert not commands._media_links
    finally:
        await _teardown()


async def test_public_base_url_override(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(commands.config, 'MEDIA_DUMP_BASE_URL',
                        'https://bot.example.com/', raising=False)
    assert await commands._public_base_url() == 'https://bot.example.com'


def test_dashboard_lists_media_dump_and_still_fits():
    assert '/media_dump' in commands.format_dashboard_message()
    assert all(len(c) <= commands.DASHBOARD_CHUNK_LIMIT
               for c in commands.format_dashboard_messages())
