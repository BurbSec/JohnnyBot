#!/usr/bin/env python3
import discord
from discord.ext import commands, tasks
import os

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'moderators'
DELAY_MINUTES = 8

if not TOKEN:
    print('DISCORD_BOT_TOKEN environment variable not set. Exiting...')
    exit(1)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    update_bad_bots.start()

@tasks.loop(minutes=1)
async def update_bad_bots():
    for guild in bot.guilds:
        bad_bots_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if bad_bots_role:
            for member in guild.members:
                if not member.bot and bad_bots_role in member.roles:
                    if len(member.roles) > 2:  # User has more than just @everyone and bad bots role
                        await member.remove_roles(bad_bots_role, reason='User has been assigned additional roles')
                        print(f'Removed {ROLE_NAME} role from {member.name} in {guild.name}')
                    elif len(member.roles) == 1:  # Newly joined user with no assigned roles
                        joined_at = member.joined_at
                        delay = DELAY_MINUTES * 60
                        if (discord.utils.utcnow() - joined_at).total_seconds() > delay:
                            await member.add_roles(bad_bots_role, reason=f'No role assigned after {DELAY_MINUTES} minutes')
                            print(f'Assigned {ROLE_NAME} role to {member.name} in {guild.name}')

@update_bad_bots.before_loop
async def before_update_bad_bots():
    await bot.wait_until_ready()

@bot.tree.command(name='post', description='Post a message in a channel')
@commands.has_role(MODERATOR_ROLE_NAME)
async def post_message(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)

@post_message.error
async def post_message_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingRole):
        await interaction.response.send_message(f'You need the {MODERATOR_ROLE_NAME} role to use this command.', ephemeral=True)
    else:
        print(f'Error occurred: {error}')

if __name__ == '__main__':
    bot.run(TOKEN)