# JohnnyBot - Discord Moderation Bot

JohnnyBot is a Discord bot designed to automate server management and enforce server rules. It provides features such as automatic role assignment, message deletion, and user management to ensure a smooth server experience. Most commands are limited to users with the MODERATOR_ROLE_NAME, however the PetBot commands can be leveraged by all users.  

## Features

- **Command-based Moderation:**
  - Provides moderators with slash commands to manage members, messages, and post announcements.

- **Reminder System:**
  - Set recurring reminders to be sent to specific channels at regular intervals
  - Persistent reminder storage with automatic scheduling

- **Event Feed Integration:**
  - Subscribe to calendar feeds and get notifications for new events
  - Configurable notification channels

- **Logging and Notifications:**
  - Logs actions and errors to a rotating log file in the bot directory
  - DM a tail of the log on request

- **Message Archive:**
  - Allows moderators to dump and archive user messages from specific channels
  - Provides temporary download links for message archives
  - Automatically cleans up old archive files

- **Channel Write Protection:**
  - Deletes any non-moderator messages posted in the `PROTECTED_CHANNELS` you define in `config.py`
  - This is a hack to get around Discord's requirement of a minimum number of messagebale channels.

- **PetBot Interactions:**
  - Includes [PetBot](https://github.com/0xMetr0/PetBot) functionality with time-themed messages

## Requirements

- Python 3.7 (tested up to 3.13)
- All modules in `requirements.txt`
- Firewall rule allowing inbound connections on port TCP port 80 (for message archive hosting)

## Concurrency Model

Note the bot uses both asyncio and threading for different purposes. DO NOT CHANGE THIS:

- **asyncio** is used for:
  - All Discord API interactions (primary event loop)
  - Background tasks (like reminder checking)
  - Command handling
  - Network operations

- **threading** is used for:
  - Synchronous operations that cannot be made async (like file I/O)
  - Thread-safe caching of Discord objects
  - Synchronization primitives (locks) for shared resources

This hybrid approach allows the bot to:
1. Handle Discord async API efficiently
2. Perform blocking operations without stalling the event loop
3. Maintain thread safety for shared resources
4. Scale well under load

## Installation

1. Clone the repository:
   ```shell
   git clone https://github.com/burbsec/johnnybot.git
   ```

2. Install the required dependencies:
   ```shell
   pip install -r requirements.txt
   ```

3. Configure the bot settings:
   - Open the config.py file in a text editor.
   - Modify the following constants according to your server's setup:
     - `MODERATOR_ROLE_NAME`: Name of the modifier role. Necessary for mods to use the bot commands.
     - `PROTECTED_CHANNELS`: Channels you wish to force to read-only when Discord requires them not to be.
     - `MODERATORS_CHANNEL_NAME`: Name of the moderators chat channel for bot notifications to be sent to.

## Running the Bot

1. Ensure all installation steps are complete.
2. Add your Discord bot token to an environment variable called `DISCORD_BOT_TOKEN` via any secure means you desire.
3. Run the bot:
   ```shell
   python bot.py
   ```
   
   You may optionally choose to run the bot as a system service that starts at boot (recommended).

4. The bot should now show as online/active in your Discord server. If not, check the logs!

## Available Commands

### 1. `/set_reminder`
**Description:** Sets a reminder message to be sent to a specified channel at regular intervals.

- **Parameters:**
  - `channel`: The target channel.
  - `title`: Title of the reminder.
  - `message`: The reminder content.
  - `interval`: Interval (in seconds) between reminders.

### 2. `/list_reminders`
**Description:** Lists all current reminders.

### 3. `/delete_all_reminders`
**Description:** Deletes all active reminders.

### 4. `/delete_reminder`
**Description:** Deletes a reminder by title.

- **Parameters:**
  - `title`: Title of the reminder to delete.

### 5. `/purge_last_messages`
**Description:** Deletes a specified number of messages from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.
  - `limit`: Number of messages to delete.

### 6. `/purge_string`
**Description:** Deletes all messages containing a specific string from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.
  - `search_string`: String to search for in messages.

### 7. `/purge_webhooks`
**Description:** Deletes all messages sent by webhooks or apps from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.

### 8. `/kick`
**Description:** Kicks a member from the server.

- **Parameters:**
  - `member`: Member to kick.
  - `reason`: Reason for the kick (optional).

### 9. `/botsay`
**Description:** Makes the bot send a message to a specified channel.

- **Parameters:**
  - `channel`: Target channel.
  - `message`: Message to send.

### 10. `/timeout`
**Description:** Timeouts a member for a specified duration.

- **Parameters:**
  - `member`: Member to timeout.
  - `duration`: Timeout duration in seconds.
  - `reason`: Reason for the timeout (optional).

### 11. `/log_tail`
**Description:** Sends the last specified number of lines from the bot's log file to the user via DM.

- **Parameters:**
  - `lines`: Number of lines to retrieve.

### 12. `/add_event_feed`
**Description:** Adds a calendar feed URL to check for events, and posts them to a channel. Adds events to Discord Server Events as well.

- **Parameters:**
  - `calendar_url`: URL of the calendar feed.
  - `channel_name`: Channel to post notifications (default: bot-trap).

### 14. `/list_event_feeds`
**Description:** Lists all registered calendar feeds.

### 15. `/remove_event_feed`
**Description:** Removes a calendar feed.

- **Parameters:**
  - `feed_url`: URL of the calendar feed to remove.

### 16. `/bot_mood`
**Description:** Check on what the PetBot is up to.

### 17. `/pet_bot`
**Description:** Pet JohnnyBot.

### 18. `/bot_pick_fav`
**Description:** See who JohnnyBot prefers today.

- **Parameters:**
  - `user1`: First potential favorite.
  - `user2`: Second potential favorite.

### 19. `/message_dump`
**Description:** Dumps a user's messages from a specified channel into a downloadable file. Compresses the file and hosts it via a temporary web server for 30 minutes. Make sure your firewall rules are set to allow inbound connections on port TCP port 80.

- **Parameters:**
  - `user`: User whose messages to dump.
  - `channel`: Channel to dump messages from.
  - `start_date`: Start date in YYYY-MM-DD format (e.g., 2025-01-01).
  - `limit`: Maximum number of messages to fetch (default: 1000).

- **Features:**
  - Retrieves messages with proper pagination
  - Handles Discord API rate limits
  - Automatically cleans up orphaned dump files
  - Provides a download link via DM
  - Link expires after 30 minutes

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

## Attribution

Bot interaction functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
