from __future__ import annotations

import datetime
import discord

from services.panel_state import PanelStateStore
from providers.lekmanga_provider import CloudflareBlockedError


C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_GREEN = discord.Color.from_rgb(87, 242, 135)
C_RED = discord.Color.from_rgb(237, 66, 69)

ERROR_CODE_MESSAGES = {
    "timeout": "⏱️ انتهت مهلة التنفيذ على Worker.",
    "download_failed": "📥 فشل تحميل الصور من المصدر.",
    "stitch_failed": "🧵 فشل دمج الصور.",
    "upload_failed": "☁️ فشل رفع النتيجة.",
    "internal_error": "⚠️ خطأ داخلي في Worker.",
}


def hf_error_message(result: dict) -> str:
    code = result.get("error_code")
    base = ERROR_CODE_MESSAGES.get(code, "❌ فشلت المهمة على Worker.")
    details = (result.get("message") or result.get("error_details") or "").strip()
    if details:
        return f"{base}\n{details[:180]}"
    return base


class UrlTitleModal(discord.ui.Modal, title="Manga Panel v2"):
    url = discord.ui.TextInput(
        label="Chapter URL",
        placeholder="https://example.com/chapter-1",
        required=True,
        max_length=1024,
    )
    title_value = discord.ui.TextInput(
        label="Title",
        placeholder="Series_Chapter_001",
        required=False,
        max_length=120,
    )

    def __init__(self, store: PanelStateStore):
        super().__init__()
        self.store = store

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        url_str = str(self.url.value).strip()
        self.store.set_url(interaction.user.id, url_str)
        self.store.set_title(interaction.user.id, str(self.title_value.value or "Manga_Chapter"))
        state = self.store.get(interaction.user.id)
        if url_str and not state.series_url:
            inferred = re.sub(r"/chapter[s]?/.*", "", url_str, flags=re.IGNORECASE)
            self.store.set_series_url(interaction.user.id, inferred)
        await interaction.response.send_message(
            f"✅ تم حفظ الرابط.\nURL: `{state.url}`\nTitle: `{state.title}`\nDestination: `{state.destination}`",
            ephemeral=True,
        )


class DestinationSelect(discord.ui.Select):
    def __init__(self, store: PanelStateStore):
        self.store = store
        options = [
            discord.SelectOption(label="Auto", value="Auto", description="Try Drive then fallback"),
            discord.SelectOption(label="Drive", value="Drive", description="Google Drive folder"),
            discord.SelectOption(label="Gofile", value="Gofile", description="Upload to Gofile"),
            discord.SelectOption(label="Catbox", value="Catbox", description="Upload to Catbox"),
            discord.SelectOption(label="Discord", value="Discord", description="Send ZIP if small"),
        ]
        super().__init__(
            placeholder="Choose upload destination",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="manga_panel_v2_destination",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        destination = self.values[0]
        self.store.set_destination(interaction.user.id, destination)
        await interaction.response.send_message(f"✅ Destination set to `{destination}`", ephemeral=True)


class SeriesUrlModal(discord.ui.Modal, title="Browse Chapters"):
    series_url = discord.ui.TextInput(
        label="Series URL",
        placeholder="https://site.com/series/xxx",
        required=True,
        max_length=1024,
    )

    def __init__(self, store: PanelStateStore):
        super().__init__()
        self.store = store

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        raw_url = str(self.series_url.value).strip()
        clean_url = re.sub(r"/chapter[s]?/.*", "", raw_url, flags=re.IGNORECASE)
        self.store.set_series_url(interaction.user.id, clean_url)
        await interaction.response.send_message("✅ Series URL saved. اضغط Browse Chapters مرة ثانية لعرض الفصول.", ephemeral=True)


class ChapterSelect(discord.ui.Select):
    def __init__(
        self,
        store: PanelStateStore,
        user_id: int,
        page_items: list[tuple[float, str]],
    ):
        self.store = store
        self.user_id = user_id
        self.page_items = page_items
        options = []
        for num, _url in page_items[:25]:
            label = f"Chapter {int(num) if float(num).is_integer() else num}"
            options.append(discord.SelectOption(label=label, value=str(num)))
        super().__init__(
            placeholder="Choose chapter",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        target = float(self.values[0])
        picked = next((row for row in self.page_items if float(row[0]) == target), None)
        if not picked:
            await interaction.response.send_message("❌ Chapter not found on current page.", ephemeral=True)
            return
        chapter, chapter_url = picked
        state = self.store.set_selected_chapter(self.user_id, chapter, chapter_url)
        state.title = f"Manga_Chapter_{int(chapter) if float(chapter).is_integer() else chapter}"
        await interaction.response.send_message(
            f"✅ Selected chapter `{chapter}`\nURL: `{state.url}`\nTitle: `{state.title}`",
            ephemeral=True,
        )


class ChapterPickerView(discord.ui.View):
    def __init__(
        self,
        store: PanelStateStore,
        user_id: int,
        chapters: list[tuple[float, str]],
        page: int = 0,
        per_page: int = 20,
    ):
        super().__init__(timeout=600)
        self.store = store
        self.user_id = user_id
        self.chapters = chapters
        self.page = page
        self.per_page = per_page
        self.total_pages = max(1, (len(chapters) + per_page - 1) // per_page)
        self._rebuild()

    def _page_items(self) -> list[tuple[float, str]]:
        start = self.page * self.per_page
        end = start + self.per_page
        return self.chapters[start:end]

    def _rebuild(self) -> None:
        self.clear_items()
        items = self._page_items()
        if items:
            self.add_item(ChapterSelect(self.store, self.user_id, items))
        self.add_item(self.PrevButton(self))
        self.add_item(self.NextButton(self))

    def build_embed(self) -> discord.Embed:
        em = discord.Embed(
            title="📚 Chapter Browser",
            color=C_BLUE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        items = self._page_items()
        if not items:
            em.description = "No chapters found."
        else:
            lines = []
            for num, _url in items:
                lbl = int(num) if float(num).is_integer() else num
                lines.append(f"• Ch. `{lbl}`")
            em.description = "\n".join(lines)
        em.set_footer(text=f"Page {self.page + 1}/{self.total_pages}")
        return em

    class PrevButton(discord.ui.Button):
        def __init__(self, parent: "ChapterPickerView"):
            super().__init__(label="Prev", style=discord.ButtonStyle.secondary)
            self.parent = parent
            self.disabled = parent.page <= 0

        async def callback(self, interaction: discord.Interaction) -> None:
            self.parent.page = max(0, self.parent.page - 1)
            self.parent._rebuild()
            await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)

    class NextButton(discord.ui.Button):
        def __init__(self, parent: "ChapterPickerView"):
            super().__init__(label="Next", style=discord.ButtonStyle.secondary)
            self.parent = parent
            self.disabled = parent.page >= parent.total_pages - 1

        async def callback(self, interaction: discord.Interaction) -> None:
            self.parent.page = min(self.parent.total_pages - 1, self.parent.page + 1)
            self.parent._rebuild()
            await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class MangaPanelV2View(discord.ui.View):
    def __init__(self, store: PanelStateStore):
        super().__init__(timeout=None)
        self.store = store
        self.add_item(DestinationSelect(store))

    def _get_status_embed(self, user_id: int, title: str = "Manga Panel v2") -> discord.Embed:
        state = self.store.get(user_id)
        em = discord.Embed(
            title=title,
            color=C_BLUE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        em.add_field(name="URL", value=state.url or "—", inline=False)
        em.add_field(name="Title", value=state.title, inline=True)
        em.add_field(name="Destination", value=state.destination, inline=True)
        em.set_footer(text="Components v2 • Persistent View")
        return em

    @discord.ui.button(
        label="Set URL + Title",
        style=discord.ButtonStyle.primary,
        custom_id="manga_panel_v2_set_url",
    )
    async def set_url_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(UrlTitleModal(self.store))

    @discord.ui.button(
        label="Browse Chapters",
        style=discord.ButtonStyle.secondary,
        custom_id="manga_panel_v2_browse",
    )
    async def browse_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.user.id)
        if not state.series_url:
            await interaction.response.send_modal(SeriesUrlModal(self.store))
            return

        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if not provider_mgr:
            await interaction.response.send_message("❌ Provider manager not configured.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            rich = await provider_mgr.get_chapters_with_lock_info(state.series_url)
            if not rich:
                await interaction.followup.send("❌ No chapters found for this series URL.", ephemeral=True)
                return
            chapters = sorted([(float(k), v.get("url", "")) for k, v in rich.items() if v.get("url")], key=lambda x: x[0], reverse=True)
            view = ChapterPickerView(self.store, interaction.user.id, chapters, page=0, per_page=20)
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to load chapters: `{str(exc)[:220]}`", ephemeral=True)

    @discord.ui.button(
        label="Start Download",
        style=discord.ButtonStyle.success,
        custom_id="manga_panel_v2_start",
    )
    async def start_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.user.id)
        if not state.url:
            await interaction.response.send_message("❌ حدد رابط الفصل أولاً من زر Set URL + Title.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        msg = await interaction.followup.send(embed=self._get_status_embed(interaction.user.id, "🔄 Starting..."), ephemeral=True)

        downloader = getattr(interaction.client, "downloader", None)
        remote_down = getattr(interaction.client, "remote_down", None)
        if not downloader:
            await msg.edit(embed=discord.Embed(title="❌ Downloader not configured", color=C_RED))
            return

        progress = {"step": "Initializing", "pct": "0%"}

        def status_embed(color: discord.Color = C_BLUE) -> discord.Embed:
            em = self._get_status_embed(interaction.user.id, f"🔄 {progress['step']}")
            em.color = color
            em.add_field(name="Progress", value=progress["pct"], inline=False)
            return em

        async def pcb(cur: int, tot: int, txt: str) -> None:
            pct = int((cur / max(tot, 1)) * 100)
            progress["step"] = txt
            progress["pct"] = f"{pct}% ({cur}/{tot})"
            try:
                await msg.edit(embed=status_embed())
            except Exception:
                pass

        try:
            result_link = None
            result_provider = "Local"
            if remote_down and remote_down.is_enabled:
                job = await remote_down.start_download(state.url, state.title)
                if "error" in job:
                    raise RuntimeError(job["error"])
                result = await remote_down.wait_for_job(job["job_id"], progress_callback=pcb)
                if result.get("status") != "completed":
                    raise RuntimeError(hf_error_message(result))
                result_link = result.get("result")
                if result_link and "drive.google.com" in result_link:
                    result_provider = "Drive"
                elif result_link and "gofile.io" in result_link:
                    result_provider = "Gofile"
                elif result_link and "catbox.moe" in result_link:
                    result_provider = "Catbox"
                else:
                    result_provider = "HF Worker"
            else:
                result = await downloader.download_and_stitch(
                    state.url,
                    state.title,
                    progress_callback=pcb,
                    upload_dest=state.destination,
                )
                if not result:
                    raise RuntimeError("Download/stitch failed")
                result_link = result.get("link")
                result_provider = result.get("type", "local")

            done = status_embed(C_GREEN)
            done.title = "✅ Completed"
            done.add_field(name="Provider", value=result_provider, inline=True)
            done.add_field(name="Result", value=result_link or "—", inline=False)
            await msg.edit(embed=done)
        except CloudflareBlockedError:
            await msg.edit(embed=discord.Embed(
                title="⛔ Cloudflare blocked this source",
                description="جرب مصدر مختلف أو Worker مساند للموقع.",
                color=C_RED,
            ))
        except Exception as exc:
            await msg.edit(embed=discord.Embed(
                title="❌ Failed",
                description=f"{str(exc)[:300]}",
                color=C_RED,
            ))
