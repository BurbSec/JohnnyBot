"""Server backup/restore: serialization filters, diff/plan matching, and
the create-vs-update apply logic that makes restore idempotent."""
import threading
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import commands


def _perms(value=0):
    p = MagicMock(spec=discord.Permissions)
    p.value = value
    # _classify_perm_target reads these dangerous-perm flags directly;
    # an unconfigured spec'd MagicMock attribute is truthy, so every
    # role would otherwise look like it has ban_members etc.
    for attr in commands._DANGEROUS_PERM_ATTRS:  # pylint: disable=protected-access
        setattr(p, attr, False)
    return p


def _role(name, permissions=0, color=0, hoist=False, mentionable=False,
          default=False, managed=False):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.permissions = _perms(permissions)
    r.colour = MagicMock(value=color)
    r.hoist = hoist
    r.mentionable = mentionable
    r.managed = managed
    r.is_default = lambda: default
    r.__ge__ = lambda self, other: False
    r.__lt__ = lambda self, other: True
    return r


def _overwrite_pair(allow=1, deny=2):
    ow = MagicMock(spec=discord.PermissionOverwrite)
    ow.pair = lambda: (_perms(allow), _perms(deny))
    return ow


def _text_channel(name, category=None, overwrites=None, topic='hi',
                   nsfw=False, slowmode_delay=0):
    ch = MagicMock(spec=discord.TextChannel)
    ch.name = name
    ch.type = discord.ChannelType.text
    ch.category = category
    ch.overwrites = overwrites or {}
    ch.topic = topic
    ch.nsfw = nsfw
    ch.slowmode_delay = slowmode_delay
    return ch


def _guild(roles=None, categories=None, channels=None, emojis=None):
    g = MagicMock()
    g.roles = roles or []
    g.categories = categories or []
    g.channels = (channels or []) + (categories or [])
    g.emojis = emojis or []
    bot_member = MagicMock()
    bot_member.top_role = _role('bot_role')
    g.me = bot_member
    return g


# ---------------------------------------------------------------------
# Command gating
# ---------------------------------------------------------------------

class _FakeTree:
    """Stand-in for discord.app_commands.CommandTree that just returns
    the decorated function, so mod_only's check can be inspected without
    needing a real bound client/tree."""

    def command(self, *, name, description):  # pylint: disable=unused-argument
        return lambda func: func


def test_reg_mod_only_gates_on_manage_messages_permission_not_role():
    """mod_only must check the manage_messages permission bit — the
    permission that lets someone delete anyone's message — not a role
    literally named MODERATOR_ROLE_NAME, and not require full
    Administrator either."""
    prev_tree = commands.tree
    commands.tree = _FakeTree()
    try:
        cmd = commands._reg(  # pylint: disable=protected-access
            'fake_mod_cmd', 'desc', lambda interaction: None, mod_only=True)
    finally:
        commands.tree = prev_tree

    checks = getattr(cmd, '__discord_app_commands_checks__', [])
    assert checks, 'mod_only=True must attach a permission check to the command'
    check = checks[0]

    inter = MagicMock()
    inter.permissions = discord.Permissions(manage_messages=False, administrator=False)
    with pytest.raises(discord.app_commands.errors.MissingPermissions):
        check(inter)

    # manage_messages alone is enough — Administrator should not be required.
    inter.permissions = discord.Permissions(manage_messages=True, administrator=False)
    assert check(inter) is True


def test_reg_admin_only_gates_on_administrator_permission_only():
    """admin_only (server_backup/restore/auto_backup) must require full
    Administrator — manage_messages alone must not be enough."""
    prev_tree = commands.tree
    commands.tree = _FakeTree()
    try:
        cmd = commands._reg(  # pylint: disable=protected-access
            'fake_admin_cmd', 'desc', lambda interaction: None, admin_only=True)
    finally:
        commands.tree = prev_tree

    checks = getattr(cmd, '__discord_app_commands_checks__', [])
    assert checks, 'admin_only=True must attach a permission check to the command'
    check = checks[0]

    inter = MagicMock()
    inter.permissions = discord.Permissions(manage_messages=True, administrator=False)
    with pytest.raises(discord.app_commands.errors.MissingPermissions):
        check(inter)

    inter.permissions = discord.Permissions(administrator=True)
    assert check(inter) is True


def test_reg_admin_only_takes_precedence_over_mod_only():
    prev_tree = commands.tree
    commands.tree = _FakeTree()
    try:
        cmd = commands._reg(  # pylint: disable=protected-access
            'fake_both_cmd', 'desc', lambda interaction: None,
            admin_only=True, mod_only=True)
    finally:
        commands.tree = prev_tree

    checks = getattr(cmd, '__discord_app_commands_checks__', [])
    assert len(checks) == 1


def test_reg_no_gate_when_mod_only_false():
    prev_tree = commands.tree
    commands.tree = _FakeTree()
    try:
        cmd = commands._reg(  # pylint: disable=protected-access
            'fake_public_cmd', 'desc', lambda interaction: None)
    finally:
        commands.tree = prev_tree

    checks = getattr(cmd, '__discord_app_commands_checks__', [])
    assert not checks


def test_is_moderator_ignores_role_name_uses_manage_messages_permission():
    """A user with a role literally named MODERATOR_ROLE_NAME but no
    manage_messages permission must not be treated as a moderator — the
    debug-log-leak decision has to match who can actually run mod
    commands now. Administrator also counts (it implies every permission)."""
    named_role_only = MagicMock()
    named_role_only.guild_permissions = discord.Permissions(
        manage_messages=False, administrator=False)

    mod_by_permission = MagicMock()
    mod_by_permission.guild_permissions = discord.Permissions(
        manage_messages=True, administrator=False)

    admin = MagicMock()
    admin.guild_permissions = discord.Permissions(administrator=True)

    assert commands._is_moderator(named_role_only) is False  # pylint: disable=protected-access
    assert commands._is_moderator(mod_by_permission) is True  # pylint: disable=protected-access
    assert commands._is_moderator(admin) is True  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------

def test_build_backup_dict_excludes_default_and_managed_roles():
    everyone = _role('@everyone', default=True)
    bot_role = _role('MyBot', managed=True)
    real = _role('Members', permissions=104324161, color=5, hoist=True)
    guild = _guild(roles=[everyone, bot_role, real])
    guild.name = 'Test Server'
    guild.id = 1

    data = commands.build_backup_dict(guild)

    names = [r['name'] for r in data['roles']]
    assert names == ['Members']
    assert data['roles'][0]['permissions'] == 104324161
    assert data['version'] == commands.BACKUP_FORMAT_VERSION


def test_serialize_overwrites_skips_member_targets():
    role = _role('Members')
    member = MagicMock(spec=discord.Member)
    overwrites = {role: _overwrite_pair(1, 2), member: _overwrite_pair(4, 8)}

    entries = commands._serialize_overwrites(overwrites)  # pylint: disable=protected-access

    assert len(entries) == 1
    assert entries[0]['target_name'] == 'Members'
    assert entries[0]['allow'] == 1
    assert entries[0]['deny'] == 2


# ---------------------------------------------------------------------
# Diff / plan
# ---------------------------------------------------------------------

def test_role_differs_true_on_permission_change():
    role = _role('Members', permissions=1, color=0)
    data = {'name': 'Members', 'permissions': 2, 'color': 0,
            'hoist': False, 'mentionable': False}
    assert commands._role_differs(role, data)  # pylint: disable=protected-access


def test_role_differs_false_when_matching():
    role = _role('Members', permissions=1, color=5, hoist=True, mentionable=True)
    data = {'name': 'Members', 'permissions': 1, 'color': 5,
            'hoist': True, 'mentionable': True}
    assert not commands._role_differs(role, data)  # pylint: disable=protected-access


def test_diff_backup_identifies_new_and_updated():
    existing = _role('Members', permissions=1, color=0)
    guild = _guild(roles=[existing])
    data = {
        'roles': [
            {'name': 'Members', 'permissions': 99, 'color': 0,
             'hoist': False, 'mentionable': False},
            {'name': 'NewRole', 'permissions': 0, 'color': 0,
             'hoist': False, 'mentionable': False},
        ],
        'categories': [{'name': 'Text Channels', 'overwrites': []}],
        'channels': [{'name': 'general', 'type': 'text', 'overwrites': []}],
        'emojis': [{'name': 'pog', 'url': 'http://x/pog.png', 'animated': False}],
    }

    plan = commands._diff_backup(guild, data)  # pylint: disable=protected-access

    assert plan['roles_update'] == ['Members']
    assert plan['roles_create'] == ['NewRole']
    assert plan['categories_create'] == ['Text Channels']
    assert plan['channels_create'] == ['general']
    assert plan['emojis_create'] == ['pog']
    assert commands._plan_has_changes(plan)  # pylint: disable=protected-access


def test_diff_backup_no_changes_when_everything_matches():
    role = _role('Members', permissions=1, color=0)
    channel = _text_channel('general')
    guild = _guild(roles=[role], channels=[channel])
    data = {
        'roles': [{'name': 'Members', 'permissions': 1, 'color': 0,
                   'hoist': False, 'mentionable': False}],
        'categories': [],
        'channels': [{'name': 'general', 'type': str(discord.ChannelType.text)}],
        'emojis': [],
    }

    plan = commands._diff_backup(guild, data)  # pylint: disable=protected-access

    assert not commands._plan_has_changes(plan)  # pylint: disable=protected-access
    assert 'No changes needed' in commands._format_restore_plan(plan)  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_backup_creates_missing_role():
    guild = _guild(roles=[])
    new_role = _role('NewRole')
    guild.create_role = AsyncMock(return_value=new_role)
    data = {'roles': [{'name': 'NewRole', 'permissions': 0, 'color': 0,
                        'hoist': False, 'mentionable': False}],
            'categories': [], 'channels': [], 'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    guild.create_role.assert_awaited_once()
    assert counts['roles_created'] == 1
    assert counts['roles_updated'] == 0
    assert counts['failed'] == 0


@pytest.mark.asyncio
async def test_apply_backup_updates_existing_role_without_duplicating():
    existing = _role('Members', permissions=1, color=0)
    existing.edit = AsyncMock()
    guild = _guild(roles=[existing])
    guild.create_role = AsyncMock()
    data = {'roles': [{'name': 'Members', 'permissions': 99, 'color': 0,
                        'hoist': False, 'mentionable': False}],
            'categories': [], 'channels': [], 'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    guild.create_role.assert_not_awaited()
    existing.edit.assert_awaited_once()
    assert counts['roles_updated'] == 1
    assert counts['roles_created'] == 0


@pytest.mark.asyncio
async def test_apply_backup_reuses_existing_channel_instead_of_creating():
    channel = _text_channel('general')
    channel.set_permissions = AsyncMock()
    guild = _guild(channels=[channel])
    guild.create_text_channel = AsyncMock()
    data = {'roles': [], 'categories': [],
            'channels': [{'name': 'general', 'type': str(discord.ChannelType.text),
                           'overwrites': []}],
            'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    guild.create_text_channel.assert_not_awaited()
    assert counts['channels_created'] == 0
    assert counts['failed'] == 0


@pytest.mark.asyncio
async def test_apply_backup_strips_dangerous_perms_from_new_role():
    """A crafted backup file requesting ban_members on a new role must
    never actually grant it — restore input is untrusted."""
    guild = _guild(roles=[])
    created = _role('Escalated')
    guild.create_role = AsyncMock(return_value=created)
    dangerous_value = discord.Permissions(ban_members=True, administrator=True).value
    data = {'roles': [{'name': 'Escalated', 'permissions': dangerous_value,
                        'color': 0, 'hoist': False, 'mentionable': False}],
            'categories': [], 'channels': [], 'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    granted = guild.create_role.call_args.kwargs['permissions']
    assert granted.administrator is False
    assert granted.ban_members is False
    assert counts['roles_dangerous_perms_stripped'] == 1


@pytest.mark.asyncio
async def test_apply_backup_never_edits_role_with_current_dangerous_perms():
    """An existing role that already has moderation perms (e.g. a real
    Moderators role) must be left alone by restore entirely, matching
    the clone_*_permissions commands' hierarchy/dangerous-perm rules."""
    mod_role = _role('Moderators', permissions=0)
    mod_role.permissions.ban_members = True
    mod_role.edit = AsyncMock()
    guild = _guild(roles=[mod_role])
    guild.create_role = AsyncMock()
    data = {'roles': [{'name': 'Moderators', 'permissions': 0, 'color': 5,
                        'hoist': False, 'mentionable': False}],
            'categories': [], 'channels': [], 'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    mod_role.edit.assert_not_awaited()
    guild.create_role.assert_not_awaited()
    assert counts['roles_skipped_unsafe'] == 1


@pytest.mark.asyncio
async def test_sync_overwrites_strips_dangerous_allow_bits():
    role = _role('Members')
    channel = _text_channel('general')
    channel.set_permissions = AsyncMock()
    dangerous_allow = discord.Permissions(manage_guild=True).value
    entries = [{'target_name': 'Members', 'allow': dangerous_allow, 'deny': 0}]

    await commands._sync_overwrites_from_data(  # pylint: disable=protected-access
        channel, entries, {'Members': role})

    applied_overwrite = channel.set_permissions.call_args.kwargs['overwrite']
    allow, _deny = applied_overwrite.pair()
    assert allow.manage_guild is False


@pytest.mark.asyncio
async def test_apply_backup_counts_unsupported_channel_type():
    guild = _guild(channels=[])
    data = {'roles': [], 'categories': [],
            'channels': [{'name': 'forum', 'type': 'forum', 'overwrites': []}],
            'emojis': []}

    counts = await commands._apply_backup(guild, data)  # pylint: disable=protected-access

    assert counts['skipped_unsupported_type'] == 1
    assert counts['failed'] == 0


# ---------------------------------------------------------------------
# Restore confirmation gate
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_confirm_view_rejects_other_users():
    view = commands._RestoreConfirmView(author_id=1)  # pylint: disable=protected-access
    inter = MagicMock()
    inter.user.id = 2
    inter.response.send_message = AsyncMock()

    allowed = await view.interaction_check(inter)

    assert allowed is False
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_confirm_view_allows_author():
    view = commands._RestoreConfirmView(author_id=1)  # pylint: disable=protected-access
    inter = MagicMock()
    inter.user.id = 1

    allowed = await view.interaction_check(inter)

    assert allowed is True


# ---------------------------------------------------------------------
# Automatic backups
# ---------------------------------------------------------------------

def _minimal_backup(role_name='Members', created_at='2026-01-01T00:00:00'):
    return {
        'version': 1, 'guild_id': 1,
        'roles': [{'name': role_name, 'permissions': 0, 'color': 0,
                   'hoist': False, 'mentionable': False}],
        'categories': [], 'channels': [], 'emojis': [],
        'created_at': created_at,
    }


def test_backup_content_hash_ignores_created_at():
    data_a = _minimal_backup(created_at='2026-01-01T00:00:00')
    data_b = _minimal_backup(created_at='2026-01-02T00:00:00')
    assert commands._backup_content_hash(data_a) == commands._backup_content_hash(data_b)  # pylint: disable=protected-access


def test_backup_content_hash_changes_with_content():
    data_a = _minimal_backup(role_name='Members')
    data_b = _minimal_backup(role_name='Others')
    assert commands._backup_content_hash(data_a) != commands._backup_content_hash(data_b)  # pylint: disable=protected-access


def test_backup_content_hash_ignores_list_order():
    """A pure drag-reorder of roles/channels isn't restorable (position
    isn't captured), so it must not register as a content change."""
    data_a = {
        'version': 1, 'guild_id': 1,
        'roles': [{'name': 'A', 'permissions': 0, 'color': 0, 'hoist': False, 'mentionable': False},
                  {'name': 'B', 'permissions': 0, 'color': 0, 'hoist': False, 'mentionable': False}],
        'categories': [], 'channels': [], 'emojis': [], 'created_at': 'x',
    }
    data_b = {
        'version': 1, 'guild_id': 1,
        'roles': [{'name': 'B', 'permissions': 0, 'color': 0, 'hoist': False, 'mentionable': False},
                  {'name': 'A', 'permissions': 0, 'color': 0, 'hoist': False, 'mentionable': False}],
        'categories': [], 'channels': [], 'emojis': [], 'created_at': 'y',
    }
    assert commands._backup_content_hash(data_a) == commands._backup_content_hash(data_b)  # pylint: disable=protected-access


def test_schedule_auto_backup_anchors_next_run_to_last_checked(monkeypatch):
    """The whole point of persisting last_checked_epoch: after a restart,
    the job must not simply fire interval-from-now again."""
    added = {}
    fake_scheduler = MagicMock()
    fake_scheduler.running = True
    fake_scheduler.add_job = lambda *args, **kwargs: added.update(kwargs)
    monkeypatch.setattr(commands, 'scheduler', fake_scheduler)

    checked_two_hours_ago = commands.time_module.time() - 7200
    cfg = {'interval_seconds': 3600, 'last_checked_epoch': checked_two_hours_ago}

    commands._schedule_auto_backup(99, cfg)  # pylint: disable=protected-access

    next_run = added['next_run_time']
    # Overdue (last check + interval is in the past) — must fire ~now,
    # not interval-seconds from now.
    assert next_run.timestamp() <= commands.time_module.time() + 5


def test_schedule_auto_backup_defers_when_not_yet_due(monkeypatch):
    added = {}
    fake_scheduler = MagicMock()
    fake_scheduler.running = True
    fake_scheduler.add_job = lambda *args, **kwargs: added.update(kwargs)
    monkeypatch.setattr(commands, 'scheduler', fake_scheduler)

    checked_five_min_ago = commands.time_module.time() - 300
    cfg = {'interval_seconds': 3600, 'last_checked_epoch': checked_five_min_ago}

    commands._schedule_auto_backup(100, cfg)  # pylint: disable=protected-access

    next_run = added['next_run_time']
    expected = checked_five_min_ago + 3600
    assert abs(next_run.timestamp() - expected) <= 5


@pytest.mark.asyncio
async def test_run_auto_backup_skips_when_hash_unchanged(monkeypatch):
    guild = _guild(roles=[_role('Members')])
    guild.id = 42
    guild.name = 'Guild42'
    guild.text_channels = []
    monkeypatch.setattr(commands, 'bot_instance', MagicMock(get_guild=MagicMock(return_value=guild)))
    monkeypatch.setattr(commands, 'auto_backup_lock', threading.Lock())
    data = commands.build_backup_dict(guild)
    same_hash = commands._backup_content_hash(data)  # pylint: disable=protected-access
    cfg = {'interval_seconds': 3600, 'last_hash': same_hash, 'last_backup_at': 'x'}
    monkeypatch.setitem(commands.auto_backup_configs, 42, cfg)
    save_calls = []
    monkeypatch.setattr(commands, '_save_backup_file',
                         lambda *a, **k: save_calls.append(1) or 'path')
    monkeypatch.setattr(commands, '_save_auto_backup_configs', lambda: None)

    await commands._run_auto_backup(42)  # pylint: disable=protected-access

    assert not save_calls
    # Checking without a delta still records that a check happened, so a
    # restart schedules the next one relative to this check, not to "now".
    assert cfg['last_checked_epoch'] > 0


@pytest.mark.asyncio
async def test_run_auto_backup_saves_and_posts_when_changed(monkeypatch, tmp_path):
    guild = _guild(roles=[_role('Members')])
    guild.id = 43
    guild.name = 'Guild43'
    channel = MagicMock()
    channel.name = commands.config.MODERATORS_CHANNEL_NAME
    channel.send = AsyncMock()
    guild.text_channels = [channel]
    monkeypatch.setattr(commands, 'bot_instance', MagicMock(get_guild=MagicMock(return_value=guild)))
    monkeypatch.setattr(commands, 'auto_backup_lock', threading.Lock())
    cfg = {'interval_seconds': 3600, 'last_hash': 'stale-hash', 'last_backup_at': None}
    monkeypatch.setitem(commands.auto_backup_configs, 43, cfg)
    backup_path = tmp_path / 'auto_43.json'
    backup_path.write_text('{}')
    monkeypatch.setattr(commands, '_save_backup_file', lambda *a, **k: str(backup_path))
    monkeypatch.setattr(commands, '_save_auto_backup_configs', lambda: None)

    await commands._run_auto_backup(43)  # pylint: disable=protected-access

    channel.send.assert_awaited_once()
    assert cfg['last_hash'] != 'stale-hash'


@pytest.mark.asyncio
async def test_auto_backup_command_disable_removes_config(monkeypatch):
    commands.auto_backup_configs[7] = {'interval_seconds': 3600, 'last_hash': None,
                                        'last_backup_at': None}
    monkeypatch.setattr(commands, 'auto_backup_lock', threading.Lock())
    monkeypatch.setattr(commands, 'scheduler', None)
    inter = MagicMock()
    inter.guild.id = 7
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await commands.auto_backup_command(inter, enabled=False)

    assert 7 not in commands.auto_backup_configs
    msg = inter.followup.send.call_args.args[0]
    assert 'disabled' in msg.lower()


@pytest.mark.asyncio
async def test_auto_backup_command_rejects_too_small_interval(monkeypatch):
    monkeypatch.setattr(commands, 'auto_backup_lock', threading.Lock())
    monkeypatch.setattr(commands, 'scheduler', None)
    inter = MagicMock()
    inter.guild.id = 8
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await commands.auto_backup_command(inter, enabled=True, interval_hours=0)

    msg = inter.followup.send.call_args.args[0]
    assert 'at least' in msg.lower()
    assert 8 not in commands.auto_backup_configs
