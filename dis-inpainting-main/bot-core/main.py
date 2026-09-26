# Main entry point for the Discord bot
import discord
from discord.ext import commands

bot = commands.Bot(command_prefix='!')

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

if __name__ == '__main__':
    bot.run('YOUR_TOKEN_HERE')
