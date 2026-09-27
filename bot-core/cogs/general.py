from __future__ import annotations

import datetime
import discord
from discord import app_commands
from discord.ext import commands

import database
from bot_config import Config
from user_system import owner_only, vip_only, user_only, get_rank, RANK_LABELS, RANK_COLORS
from ui.components_v2 import InteractiveHelpView, build_status_layout


C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_TEAL = discord.Color.from_rgb(32, 178, 170)
C_PURPLE = discord.Color.from_rgb(124, 92, 252)


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="لوحة المساعدة والخدمات التفاعلية الموحدة")
    @user_only()
    async def help_cmd(self, interaction: discord.Interaction):
        rank = await get_rank(interaction.user.id)
        view = InteractiveHelpView(self.bot, rank)
        await interaction.response.send_message(view=view, ephemeral=True)

    @commands.command()
    async def sync(self, ctx: commands.Context):
        if Config.is_allowed(ctx.author.id):
            synced = await self.bot.tree.sync()
            await ctx.send(f"✅ تمت مزامنة {len(synced)} أمر.")
            await database.log_event("OK", f"Commands synced by {ctx.author.id}")

async def setup(bot: commands.Bot):
    await bot.add_cog(GeneralCog(bot))
