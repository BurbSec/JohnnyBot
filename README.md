# JohnnyBot - Discord Moderation Bot

JohnnyBot is a Discord moderation bot designed to automate role management and enforce server rules. It provides features such as automatic role assignment, message deletion, and user management to ensure a smooth server experience.

## Features

- **Automatic Role Management:**
  - Assigns the "bad bots" role to new members automatically.
  - Removes the "bad bots" role from members who complete onboarding.
- **Moderation Automation:**
  - Deletes messages sent by members with the "bad bots" role in non-DM channels.
  - Kicks members with the "bad bots" role who violate server guidelines.
  - Bans members with the "bad bots" role or no role who DM the bot, deleting their messages across the server.
- **Command-based Moderation:**
  - Provides moderators with slash commands to manage members, messages, and post announcements.
- **Logging and Notifications:**
  - Logs actions and errors to a rotating log file.
  - Sends notifications to a designated moderators-only channel.

## Requirements

- Python 3.7 or higher
- Official Discord.py library version 2.4 or higher

## Installation

1. Clone the repository:
   ```shell
   git clone https://github.com/yourusername/johnnybot.git
   ```

2. Install the required dependencies:
   ```shell
   pip install -r requirements.txt
   ```

3. Set up the token:
   - Get a bot token by following [this guide](https://www.writebots.com/discord-bot-token/)
   - Create a new file named `.env` in the project directory.
   - Add the following line to the `.env` file, replacing `YOUR_BOT_TOKEN` with your actual Discord bot token:
     ```shell
     DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
     ```

4. Configure the bot settings:
   - Open the `bot.py` file in a text editor.
   - Modify the following constants according to your server's setup:
     - `BAD_BOT_ROLE_NAME`: Role assigned to bad bots (default: 'bad bots').
     - `MODERATOR_ROLE_NAME`: Moderator role name (default: 'Moderators').
     - `DELAY_MINUTES`: Delay before assigning the "bad bots" role to new members (default: 4 minutes).
     - `LOG_FILE`: Name of the log file (default: 'johnnybot.log').
     - `MODERATORS_CHANNEL_NAME`: Name of the moderators channel for notifications (default: 'moderators_only').

## Running the Bot

1. Ensure all installation steps are complete.
2. Run the bot:
   ```shell
   python bot.py
   ```
3. The bot should now be online and moderating your Discord server.

## Available Commands

### 1. `/set_reminder`
**Description:** Sets a reminder message to be sent to a specified channel at regular intervals.
- **Parameters:**
  - `channel`: The target channel.
  - `title`: Title of the reminder.
  - `message`: The reminder content.
  - `interval`: Interval (in seconds) between reminders.

### 2. `/purge`
**Description:** Deletes a specified number of messages from a channel.
- **Parameters:**
  - `channel`: The channel to purge messages from.
  - `limit`: Number of messages to delete.

### 3. `/mute`
**Description:** Mutes a member by adding a specific role.
- **Parameters:**
  - `member`: Member to mute.
  - `reason`: Reason for the mute (optional).

### 4. `/kick`
**Description:** Kicks a member from the server.
- **Parameters:**
  - `member`: Member to kick.
  - `reason`: Reason for the kick (optional).

### 5. `/botsay`
**Description:** Makes the bot send a message to a specified channel.
- **Parameters:**
  - `channel`: Target channel.
  - `message`: Message to send.

### 6. `/timeout`
**Description:** Timeouts a member for a specified duration.
- **Parameters:**
  - `member`: Member to timeout.
  - `duration`: Timeout duration in seconds.
  - `reason`: Reason for the timeout (optional).

### 7. `/log_tail`
**Description:** Sends the last specified number of lines from the bot's log file to the user via DM.
- **Parameters:**
  - `lines`: Number of lines to retrieve.

## Usage

- The bot automatically manages roles and moderates the server based on the configured rules.
- Moderators can perform actions using the slash commands listed above.
- Log files are accessible, and the bot can DM logs on request through the `/log_tail` command.

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
