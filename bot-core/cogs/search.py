from __future__ import annotations

import datetime
import discord
from discord import app_commands
from discord.ext import commands

from user_system import user_only


C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_RED = discord.Color.from_rgb(237, 66, 69)


class SearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="search", description="البحث عن المانجا والسلاسل من مختلف المصادر")
    @app_commands.describe(query="اسم السلسلة أو المانجا المراد البحث عنها")
    @user_only()
    async def search_manga(self, interaction: discord.Interaction, query: str):
        metrics = getattr(self.bot, "metrics", None)
        await interaction.response.defer()
        provider_mgr = getattr(self.bot, "provider_mgr", None)
        if not provider_mgr:
            if metrics:
                metrics.inc("search_fail")
            return await interaction.followup.send("❌ Provider manager غير متاح حالياً.", ephemeral=True)

        results = await provider_mgr.search_manga(query, limit=10)
        if not results:
            if metrics:
                metrics.inc("search_fail")
            return await interaction.followup.send(embed=discord.Embed(
                title="🔍 لا نتائج",
                description=f"لم يُعثر على نتائج لـ `{query}`.",
                color=C_RED,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            ))
        if metrics:
            metrics.inc("search_ok")

        em = discord.Embed(
            title=f"🔍 نتائج البحث: {query}",
            description="اختر مانجا لفتح لوحة التحكم:",
            color=C_BLUE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        class SearchDropdown(discord.ui.Select):
            def __init__(self, items):
                options = [
                    discord.SelectOption(
                        label=r["title"][:100],
                        description=f"Status: {r['status']}",
                        value=r["url"],
                        emoji="📖",
                    )
                    for r in items
                ]
                super().__init__(placeholder="اختر مانجا...", options=options)

            async def callback(self, i: discord.Interaction):
                await i.response.defer()
                radar_cog = self.view.bot.get_cog("RadarCog")  # type: ignore[attr-defined]
                if radar_cog:
                    await radar_cog.manga_panel_cmd(i, self.values[0])
                else:
                    await i.followup.send("❌ وحدة الرادار غير متاحة.", ephemeral=True)

        class SearchView(discord.ui.View):
            def __init__(self, bot: commands.Bot, items):
                super().__init__(timeout=180)
                self.bot = bot
                self.add_item(SearchDropdown(items))

        view = SearchView(self.bot, results)

        cover = (results[0].get("cover") or "").strip()
        if cover.startswith("http://") or cover.startswith("https://"):
            em.set_thumbnail(url=cover)

        await interaction.followup.send(embed=em, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(SearchCog(bot))
