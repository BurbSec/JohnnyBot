"""Anti-nuke: audit-log burst detection, permission escalation, response."""
from unittest.mock import MagicMock, AsyncMock

import pytest
import discord

import bot
import config


def _guild(guild_id=1, owner_id=42):
    g = MagicMock()
    g.id = guild_id
    g.name = f'guild-{guild_id}'
    g.owner_id = owner_id
    mod_channel = MagicMock()
    mod_channel.name = 'moderators_only'
    mod_channel.send = AsyncMock()
    g.text_channels = [mod_channel]
    return g, mod_channel


def _actor_member(guild, member_id=100, permissions=None):
    m = MagicMock()
    m.id = member_id
    m.edit = AsyncMock()
    # Explicit Permissions, not a bare MagicMock: the escalation check
    # reads these to decide whether the actor already held what was
    # granted, and a MagicMock attribute is truthy for every permission.
    m.guild_permissions = (
        discord.Permissions.none() if permissions is None else permissions)
    guild.get_member.side_effect = lambda uid: m if uid == member_id else None
    return m


def _entry(guild, action, user_id=100):
    e = MagicMock()
    e.guild = guild
    e.action = action
    e.user_id = user_id
    e.user = f'actor-{user_id}'
    e.target = MagicMock()
    e.target.edit = AsyncMock()
    e.target.remove_roles = AsyncMock()
    # _revert_escalation resolves a role_update target through the guild,
    # because the audit log hands back a bare discord.Object when the role
    # isn't cached.
    guild.get_role.return_value = e.target
    # No permission changes by default — plain MagicMock attributes would
    # otherwise look like a granted Permissions object.
    e.before = MagicMock(spec=[])
    e.after = MagicMock(spec=[])
    return e


@pytest.fixture(autouse=True)
def _reset_state():
    bot._nuke_actions.clear()
    # Bot.user is a read-only property over _connection.user, which is a
    # plain attribute — this is how the tests give the bot an identity to
    # compare audit-log actors against.
    previous = bot.bot._connection.user
    bot.bot._connection.user = MagicMock(id=999)
    yield
    bot.bot._connection.user = previous
    bot._nuke_actions.clear()


# ── burst detection ──

@pytest.mark.asyncio
async def test_burst_below_threshold_is_quiet():
    g, mod_channel = _guild()
    _actor_member(g)
    for _ in range(2):  # threshold is 3
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_burst_past_threshold_strips_roles_and_alerts():
    g, mod_channel = _guild()
    actor = _actor_member(g)
    for _ in range(3):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))

    actor.edit.assert_awaited_once()
    assert actor.edit.call_args.kwargs['roles'] == []
    mod_channel.send.assert_awaited_once()
    sent = mod_channel.send.call_args[0][0]
    assert 'ANTI-NUKE TRIGGERED' in sent
    assert 'Stripped all roles' in sent


@pytest.mark.asyncio
async def test_bots_own_actions_are_never_counted():
    """Regression guard: /server_restore, /raid kick_recent etc. are
    attributed to the bot itself and must not trip anti-nuke."""
    g, mod_channel = _guild()
    _actor_member(g)
    for _ in range(10):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete, user_id=999))
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_non_destructive_actions_are_ignored():
    g, mod_channel = _guild()
    _actor_member(g)
    for _ in range(10):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.message_pin))
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_actions_by_different_actors_do_not_aggregate():
    g, mod_channel = _guild()
    _actor_member(g)
    for uid in (100, 101, 102):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.ban, user_id=uid))
    mod_channel.send.assert_not_called()


# ── permission escalation ──

@pytest.mark.asyncio
async def test_role_update_granting_admin_fires_immediately():
    g, mod_channel = _guild()
    actor = _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=discord.Permissions.none())
    entry.after = MagicMock(
        permissions=discord.Permissions(administrator=True))

    await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_awaited_once()
    sent = mod_channel.send.call_args[0][0]
    assert 'permission escalation' in sent
    assert 'administrator' in sent
    actor.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_role_update_without_new_dangerous_perm_is_quiet():
    g, mod_channel = _guild()
    _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=discord.Permissions.none())
    entry.after = MagicMock(
        permissions=discord.Permissions(send_messages=True))

    await bot.on_audit_log_entry_create(entry)
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_already_held_permission_is_not_treated_as_a_grant():
    g, mod_channel = _guild()
    _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(
        permissions=discord.Permissions(administrator=True))
    entry.after = MagicMock(
        permissions=discord.Permissions(
            administrator=True, send_messages=True))

    await bot.on_audit_log_entry_create(entry)
    mod_channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_assigning_a_privileged_role_fires():
    g, mod_channel = _guild()
    _actor_member(g)
    privileged = MagicMock()
    privileged.permissions = discord.Permissions(ban_members=True)
    entry = _entry(g, discord.AuditLogAction.member_role_update)
    entry.after = MagicMock(roles=[privileged])

    await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_awaited_once()
    assert 'ban_members' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_granting_a_permission_the_actor_already_holds_is_routine():
    """A real admin building out a Moderator role must not get locked out
    of their own server — you cannot escalate to what you already have."""
    g, mod_channel = _guild()
    # Member.guild_permissions already folds in the administrator
    # implication, so a real admin's permissions are Permissions.all().
    actor = _actor_member(g, permissions=discord.Permissions.all())
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=discord.Permissions.none())
    entry.after = MagicMock(
        permissions=discord.Permissions(kick_members=True))

    await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_not_called()
    actor.edit.assert_not_called()


# ── reverting the escalation itself ──

@pytest.mark.asyncio
async def test_role_update_escalation_is_reverted():
    """Stripping the actor isn't enough — the role would keep the
    permission, and so would everyone else holding it."""
    g, mod_channel = _guild()
    _actor_member(g)
    before_perms = discord.Permissions.none()
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=before_perms)
    entry.after = MagicMock(
        permissions=discord.Permissions(administrator=True))

    await bot.on_audit_log_entry_create(entry)

    entry.target.edit.assert_awaited_once()
    assert entry.target.edit.call_args.kwargs['permissions'] == before_perms
    assert 'Reverted' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_privileged_role_assignment_is_removed_from_the_target():
    """Otherwise the alt keeps administrator and the attack succeeded."""
    g, mod_channel = _guild()
    _actor_member(g)
    privileged = MagicMock()
    privileged.permissions = discord.Permissions(administrator=True)
    entry = _entry(g, discord.AuditLogAction.member_role_update)
    entry.after = MagicMock(roles=[privileged])

    await bot.on_audit_log_entry_create(entry)

    entry.target.remove_roles.assert_awaited_once()
    assert entry.target.remove_roles.call_args[0][0] is privileged
    assert 'Removed' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_uncached_role_target_does_not_crash_the_handler():
    """The audit log hands back a bare discord.Object for an uncached
    role — the realistic case being one the attacker just created."""
    g, mod_channel = _guild()
    _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=discord.Permissions.none())
    entry.after = MagicMock(
        permissions=discord.Permissions(administrator=True))
    entry.target = discord.Object(id=12345)
    g.get_role.return_value = None  # not in cache

    await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_awaited_once()
    assert 'no longer resolvable' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_failed_revert_is_reported_not_raised():
    g, mod_channel = _guild()
    _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.role_update)
    entry.before = MagicMock(permissions=discord.Permissions.none())
    entry.after = MagicMock(
        permissions=discord.Permissions(administrator=True))
    entry.target.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'no rank'))

    await bot.on_audit_log_entry_create(entry)

    assert 'Could not revert' in mod_channel.send.call_args[0][0]


# ── member prune ──

@pytest.mark.asyncio
async def test_single_prune_fires_without_reaching_the_burst_threshold():
    """One prune entry can remove hundreds of members, so it never gets
    to accumulate toward a threshold."""
    g, mod_channel = _guild()
    actor = _actor_member(g)
    entry = _entry(g, discord.AuditLogAction.member_prune)
    entry.extra = MagicMock(members_removed=250)

    await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_awaited_once()
    sent = mod_channel.send.call_args[0][0]
    assert 'member prune' in sent
    assert '250' in sent
    actor.edit.assert_awaited_once()


# ── response modes ──

@pytest.mark.asyncio
async def test_alert_mode_does_not_touch_roles(monkeypatch):
    monkeypatch.setattr(config, 'ANTI_NUKE_ACTION', 'alert')
    g, mod_channel = _guild()
    actor = _actor_member(g)
    for _ in range(3):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.role_delete))

    actor.edit.assert_not_called()
    mod_channel.send.assert_awaited_once()
    assert 'ANTI-NUKE TRIGGERED' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_guild_owner_is_never_stripped():
    g, mod_channel = _guild(owner_id=100)
    actor = _actor_member(g, member_id=100)
    for _ in range(3):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete, user_id=100))

    actor.edit.assert_not_called()
    assert 'guild owner' in mod_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_forbidden_strip_falls_back_to_alert_text():
    g, mod_channel = _guild()
    actor = _actor_member(g)
    actor.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'hierarchy'))
    for _ in range(3):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))

    sent = mod_channel.send.call_args[0][0]
    assert 'Could not strip roles' in sent


@pytest.mark.asyncio
async def test_failed_strip_keeps_alerting_on_continued_destruction():
    """If the strip failed the actor still has full permissions — the
    counter must not reset, or their ongoing spree goes quiet until a
    whole fresh window accumulates."""
    g, mod_channel = _guild()
    actor = _actor_member(g)
    actor.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'hierarchy'))
    for _ in range(5):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))

    # Fires on the 3rd, and again on the 4th and 5th — not silenced.
    assert mod_channel.send.await_count == 3


@pytest.mark.asyncio
async def test_successful_strip_resets_the_counter():
    g, mod_channel = _guild()
    _actor_member(g)
    for _ in range(5):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))

    # Fires once at 3, counter clears, 4 and 5 rebuild toward the next one.
    assert mod_channel.send.await_count == 1


@pytest.mark.asyncio
async def test_repeated_prunes_are_deduped():
    g, mod_channel = _guild()
    actor = _actor_member(g)
    actor.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'hierarchy'))
    for _ in range(3):
        entry = _entry(g, discord.AuditLogAction.member_prune)
        entry.extra = MagicMock(members_removed=100)
        await bot.on_audit_log_entry_create(entry)

    mod_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_protection_skips_everything(monkeypatch):
    monkeypatch.setattr(config, 'ANTI_NUKE_ENABLED', False)
    g, mod_channel = _guild()
    actor = _actor_member(g)
    for _ in range(10):
        await bot.on_audit_log_entry_create(
            _entry(g, discord.AuditLogAction.channel_delete))

    actor.edit.assert_not_called()
    mod_channel.send.assert_not_called()
