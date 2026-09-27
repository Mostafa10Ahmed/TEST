from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.panel_state import PanelStateStore
from ui.manga_panel_v2 import MangaPanelV2View
from user_system import vip_only


class PanelV2Cog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = PanelStateStore()
        self.view = MangaPanelV2View(self.store)

    async def cog_load(self) -> None:
        # Persistent view for components v2
        self.bot.add_view(self.view)

    @app_commands.command(name="manga_panel", description="فتح لوحة التفاعل المتقدمة لاختيار وتحميل الفصول")
    @vip_only()
    async def manga_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "لوحة التحكم التفاعلية جاهزة. اضبط الرابط ثم ابدأ التحميل:",
            view=self.view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelV2Cog(bot))
