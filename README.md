# JohnnyBot - The Missing Discord Server Management Toolkit

JohnnyBot does all of the stuff Discord bizarrely won't let you do!
Designed to automate tons of server management and enforce some rules while
you're at it. It provides features such as mass role management, message
moderation, permissions cloning, and event feed integration to ensure a smooth
server experience. Most moderation commands require the "Manage Messages"
permission (no role to create or name — works on any server out of the
box); server backup and restore commands require the stricter
Administrator permission. PetBot commands can be leveraged by all users.

## Documentation

**For complete documentation, installation guides, and command references, visit the [JohnnyBot Wiki](../../wiki).**

### Quick Links
- **[Wiki Home](../../wiki/Home)** - Project overview and features
- **[Setup Guide](../../wiki/Setup-Guide)** - Complete installation and configuration
- **[Commands Reference](../../wiki/Commands-Reference)** - All available commands with examples

## Automatic Behavior

These run without being invoked, so they are worth knowing about before you deploy:

- **DMs to the bot get you kicked.** Anyone who sends the bot a direct message is removed from every server they share with it, and the kick is reported to the moderators channel. Two exemptions: users with the Manage Messages permission, and anyone the bot itself DMed in the last 24 hours — so replying to a `/message_dump` archive or `/log_tail` output is safe.
- **Protected channels are enforced.** Messages posted by anyone without Manage Messages in any channel listed in `PROTECTED_CHANNELS` are deleted.
- **Voice channel chaperone.** When a voice channel contains exactly one adult and one child (by `ADULT_ROLE_NAMES` / `CHILD_ROLE_NAMES`), everyone in it is server-muted and the moderators channel is alerted once. The mute lifts automatically when the channel is no longer one adult and one child, when a muted member moves to a safe channel, or when the feature is disabled — including across a bot restart, since outstanding mutes are persisted to `chaperone_mutes.json`. Only mutes the bot applied are lifted; a manual moderator mute is never undone. Toggle with `/voice_chaperone`.

## Key Features

### Moderation

| Command | Description | Access |
|---|---|---|
| `/purge_last_messages` | Purge a specified number of messages from a channel | Mod |
| `/purge_string` | Purge all messages containing a specific string from a channel | Mod |
| `/purge_webhooks` | Purge all messages sent by webhooks or apps from a channel | Mod |
| `/kick` | Kick one or more members from the server | Mod |
| `/kick_role` | Kick all members with a specified role from the server | Mod |
| `/timeout` | Timeout a member for a specified duration | Mod |
| `/botsay` | Make the bot send a message to a specified channel | Mod |
| `/message_dump` | Dump a user's messages from a channel into a zipped archive DM'd to you (25 MB cap) | Mod |

### Permissions Management

| Command | Description | Access |
|---|---|---|
| `/clone_category_permissions` | Clone permission overwrites from one category to another | Mod |
| `/clone_channel_permissions` | Clone permission overwrites from one channel to another | Mod |
| `/clone_role_permissions` | Clone permissions from one role to another (moderation permissions — Administrator, ban/kick, manage roles/guild/channels/messages, timeout — are excluded and must be granted manually) | Mod |
| `/clear_category_permissions` | Clear all permission overwrites from a category | Mod |
| `/clear_channel_permissions` | Clear all permission overwrites from a channel | Mod |
| `/clear_role_permissions` | Reset a role's permissions to default | Mod |
| `/sync_channel_perms` | Sync all channels in a category to match the category's permissions | Mod |

### Role Management

| Command | Description | Access |
|---|---|---|
| `/assign_role` | Mass-assign a role to multiple users at once | Mod |
| `/remove_role` | Mass-remove a role from multiple users at once | Mod |
| `/list_users_without_roles` | List all users with no server roles assigned | Mod |

### Reminders

| Command | Description | Access |
|---|---|---|
| `/set_reminder` | Set a recurring reminder message to a channel at a specified interval | Mod |
| `/list_reminders` | List all active reminders | All |
| `/delete_reminder` | Delete a reminder by title | Mod |
| `/delete_all_reminders` | Delete all active reminders | Mod |

### Event Feeds

| Command | Description | Access |
|---|---|---|
| `/add_event_feed` | Subscribe to an iCal or RSS feed (including Meetup.com); auto-detects feed type, runs an immediate check, creates Discord Scheduled Events with duplicate detection, and enables automatic event announcements to the chosen channel | Mod |
| `/list_event_feeds` | List all registered event feeds | All |
| `/remove_event_feed` | Remove a feed by name (announcements are disabled when the last feed is removed) | Mod |
| `/check_event_feeds` | Manually trigger a check of all feeds for new events | Mod |

Once a feed is added, everything else is automatic: feeds are re-checked every Monday at 9am, a "This Week" preview of the server's Discord Scheduled Events posts Monday at 10am, and day-of reminders post Tuesday through Sunday at 10am (Monday is skipped because the weekly preview already covers that day's events). Timezone is set by `BOT_TIMEZONE` in `config.py`, default US Central.

### Autoreply

| Command | Description | Access |
|---|---|---|
| `/autoreply add` | Add an autoreply rule with a trigger string, reply text, and optional case sensitivity | Mod |
| `/autoreply list` | List all autoreply rules for this server | All |
| `/autoreply remove` | Remove an autoreply rule by ID | Mod |
| `/autoreply toggle` | Enable or disable an autoreply rule by ID | Mod |

### Server Backup

| Command | Description | Access |
|---|---|---|
| `/server_backup` | Create a full structural backup of the server (roles, categories, channels, permission overwrites, emoji) and DM it to you as a JSON file | Admin |
| `/server_restore` | Restore server structure from a backup file. Shows a diff-style preview and requires an explicit confirm click before anything changes; automatically creates a safety snapshot of the current state first. Matches existing roles/categories/channels by name, so re-running is idempotent rather than duplicating everything. Never touches Administrator/managed/above-hierarchy roles or their overwrites, and never restores message history, member-specific overwrites, or audit logs | Admin |
| `/auto_backup` | Enable/disable automatic backups on an interval (default 24h). A new backup is only created — and posted to the moderators channel — when the server's structure actually changed since the last one | Admin |

### System & Utilities

| Command | Description | Access |
|---|---|---|
| `/voice_chaperone` | Enable/disable automatic voice channel safety monitoring (when only 1 adult + 1 child are in a channel, everyone present is server-muted and mods are alerted; the mute lifts automatically once the channel is no longer 1 adult + 1 child) | Mod |
| `/log_tail` | DM the last N lines of the bot log to yourself | Mod |
| `/dashboard` | Display all available commands grouped by category | All |

`Mod` above means the invoker needs the **Manage Messages** permission — not a specific role name, so there's nothing to configure to make command access work on a new server. `Admin` means the stricter **Administrator** permission, reserved for the backup/restore commands since they can rewrite the server's entire structure.

Update checking is configured in `config.py` (not via slash command): `UPDATE_CHECKING_ENABLED` turns on daily checks for new commits with moderator notifications, and `AUTO_UPDATE_ENABLED` additionally makes the bot pull CI-passing updates and restart itself. Notifications display a git tag (e.g. `v1.0.0`) when the relevant commit is tagged, falling back to a short commit SHA otherwise — tags are cosmetic labels only, update detection itself is still commit-based so untagged commits are never missed.

### PetBot Interactions

| Command | Description | Access |
|---|---|---|
| `/bot_mood` | Check the bot's current mood | All |
| `/pet_bot` | Pet the bot | All |
| `/bot_pick_fav` | See who the bot prefers between two users today | All |

## Quick Start

1. **Clone and Install:**
   ```shell
   git clone https://github.com/burbsec/johnnybot.git
   cd johnnybot
   pip install -r requirements.txt
   ```

2. **Configure:** Copy [`config_example.py`](config_example.py) to `config.py` and edit it with your server settings (`config.py` is gitignored)

3. **Set Token:** Add your Discord bot token as environment variable:
   ```shell
   export DISCORD_BOT_TOKEN="your_bot_token_here"
   ```

4. **Run:** `python bot.py`

**Need help?** Check the **[Setup Guide](../../wiki/Setup-Guide)** for detailed instructions.

## Requirements

- **Python 3.11+** (tested on 3.11 and 3.12 in CI)
- **Dependencies:** Listed in [`requirements.txt`](requirements.txt)

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

For detailed development information, see the **[Wiki](../../wiki)**.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

**Attribution:** Bot interaction functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.
