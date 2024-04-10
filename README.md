# JohnnyBot - Discord Moderation Bot

JohnnyBot is a Discord moderation bot designed to automate role management and enforce server rules. It provides features such as automatic role assignment, message deletion, and user banning for members who violate the server's guidelines.

## Features

- Automatically assigns the "bad bots" role to new members who haven't been assigned any roles after a specified delay.
- Removes the "bad bots" role from members who have been assigned additional roles.
- Deletes messages sent by members with the "bad bots" role in non-DM channels.
- Bans members with the "bad bots" role or no role who DM the bot, and deletes all their messages in the server.
- Provides a slash command for moderators to post messages in a specified channel.
- Logs all actions and errors to a rotating log file and sends notifications to a designated moderators channel.

## Requirements

- Python 3.7 or higher
- discord.py library (version 2.0 or higher)

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
     - `BAD_BOT_ROLE_NAME`: The name of the role assigned to bad bots (default: 'bad bots').
     - `MODERATOR_ROLE_NAME`: The name of the moderator role (default: '(1337) Moderators').
     - `DELAY_MINUTES`: The delay in minutes before assigning the "bad bots" role to new members (default: 1).
     - `LOG_FILE`: The name of the log file (default: 'johnnybot.log').
     - `MODERATORS_CHANNEL_NAME`: The name of the moderators channel for notifications (default: 'moderators_only').

## Running the Bot

1. Make sure you have completed the installation steps.

2. Run the bot:

   ```shell
   python bot.py
   ```

3. The bot should now be online and ready to moderate your Discord server.

## Usage

- The bot automatically performs role management and moderation tasks based on the configured settings.
- Moderators can use the `/post` slash command to send messages to a specified channel.
- Logs and notifications are sent to the designated moderators channel and stored in the log file.

## Contributing

Contributions are welcome! If you find any bugs or have suggestions for improvements, please open an issue or submit a pull request.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).