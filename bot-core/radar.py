import asyncio
import datetime
import os
import random
import re
from typing import Optional

import database
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from download_ui import LOGO_DRIVE, LOGO_GOFILE
from manga_downloader import MangaDownloader

# Radar v2 UI
from services.intervals import format_check_interval, parse_check_interval
from services.adaptive_poller import AdaptivePoller
from services.feed_watcher import FeedWatcher
from ui.components_v2 import (
    add_cover_media,
    add_select_row,
    build_download_completed_container,
    format_chapter_line,
)
from ui.radar_card_v2 import TrackerCardV2View, build_tracker_card_layout, build_tracker_batch_card_layout
from ui.radar_panel_v2 import RadarPanelV2View
from user_system import get_rank, owner_only, vip_only

RADAR_CONCURRENT = 3
CHAPTERS_PER_PAGE = 20
MAX_SELECT_PER_JOB = 100  # حد أقصى للفصول في طلب واحد (مثل ARCANE)
DL_CONCURRENT = 2
GLOBAL_DL_LIMIT = 5
PING_DELETE_AFTER_SEC = 12

# سيمفور عالمي للتحكم في استهلاك موارد السيرفر (RAM/CPU)
GLOBAL_SEMAPHORE = asyncio.Semaphore(GLOBAL_DL_LIMIT)

# ── ألوان ─────────────────────────────────────────────────────────────────
C_IDLE = discord.Color.from_rgb(43, 45, 49)
C_RUN = discord.Color.from_rgb(88, 101, 242)
C_DONE = discord.Color.from_rgb(35, 165, 89)
C_FAIL = discord.Color.from_rgb(242, 63, 66)
C_RADAR = discord.Color.from_rgb(114, 137, 218)
C_GREY = discord.Color.from_rgb(148, 156, 164)
C_INFO = discord.Color.from_rgb(0, 168, 252)
C_PANEL = discord.Color.from_rgb(255, 193, 7)

LOGO_BOT = "https://cdn.discordapp.com/embed/avatars/0.png"

# ── أيقونات الحالة ─────────────────────────────────────────────────────────
ICO = {
    "idle": "⬛",
    "selected": "🟦",
    "locked": "🔒",
    "queued": "⏳",
    "downloading": "📥",
    "stitching": "🧵",
    "uploading": "📤",
    "done": "✅",
    "failed": "❌",
}


def pbar(pct: int, length: int = 16) -> str:
    filled = int(round(pct / 100 * length))
    empty = length - filled
    return f"{'█' * filled}{'░' * empty}  {pct:>3}%"


def _lbl(num) -> str:
    return str(int(num)) if float(num).is_integer() else str(num)


def _series_name(url: str) -> str:
    if "?" in url:
        url = url.split("?")[0]
    if "#" in url:
        url = url.split("#")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    ignored = {"status", "detail", "chapters", "list", "webtoon", "manga", "series"}
    while parts and parts[-1].lower() in ignored:
        parts.pop()
    return parts[-1].replace("-", " ").replace("_", " ").title() if parts else "Manga"


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _resolve_subscription_mention(guild: discord.Guild, title: str, original_mention: str = "") -> str:
    role_name = f"🔔 Sub: {title[:80]}"
    role = discord.utils.get(guild.roles, name=role_name)
    role_mention = role.mention if role else ""
    
    mentions = []
    if original_mention:
        mentions.append(original_mention)
    if role_mention:
        mentions.append(role_mention)
        
    return " ".join(mentions).strip()


def _build_notify_mentions(
    owner: discord.User,
    notify_user: Optional[discord.User] = None,
    notify_role: Optional[discord.Role] = None,
    mention_owner: bool = True,
) -> str:
    """Build a safe mention string for radar notifications."""
    mentions: list[str] = []
    if mention_owner and owner:
        mentions.append(owner.mention)
    if notify_user:
        mentions.append(notify_user.mention)
    if notify_role:
        mentions.append(notify_role.mention)

    seen: set[str] = set()
    deduped: list[str] = []
    for mention in mentions:
        if (
            mention
            and mention not in seen
            and "@everyone" not in mention
            and "@here" not in mention
        ):
            seen.add(mention)
            deduped.append(mention)
    return " ".join(deduped)


def _tracker_interval_minutes(row: tuple) -> int:
    if len(row) > 12 and row[12]:
        return max(1, int(row[12]))
    return max(60, int(row[6] or 1) * 60)


def _batch_chapter_filename(num: float) -> str:
    """اسم ملف مرتب داخل مجلد Gofile/Drive (Ch_00061 …)."""
    if float(num).is_integer():
        return f"Ch_{int(num):05}"
    return f"Ch_{str(num).replace('.', '_')}"


def _batch_upload_dest(upload_dest: str) -> str | None:
    """وجهة المجلد المجمع للتحميلات المتعددة."""
    from bot_config import Config

    if upload_dest == "Drive":
        return "Drive"
    if upload_dest == "Gofile":
        return "Gofile"
    if upload_dest == "Auto":
        if Config.GOOGLE_DRIVE_FOLDER_ID and Config.GOOGLE_SERVICE_ACCOUNT_JSON:
            return "Drive"
        if Config.GOFILE_TOKEN:
            return "Gofile"
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Modal — نطاق الفصول
# ─────────────────────────────────────────────────────────────────────────
class RangeModal(ui.Modal, title="Select chapter range"):
    text = ui.TextInput(
        label="Chapter numbers/ranges",
        placeholder="e.g. 1-5, 10, 15.5  |  latest:10",
        min_length=1,
        max_length=120,
        style=discord.TextStyle.short,
    )

    def __init__(self, panel: "MangaPanelView"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.text.value.strip()
        nums = set(self.panel.all_chapters)
        sel: set[float] = set()

        ordered = self.panel.all_chapters

        try:
            if raw.lower().startswith("latest:"):
                n = int(raw.split(":")[1])
                sel = set(ordered[:n])
            else:
                for part in raw.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        sub_parts = part.split("-")
                        if len(sub_parts) == 2:
                            lo = float(sub_parts[0].strip())
                            hi = float(sub_parts[1].strip())
                            if lo > hi:
                                lo, hi = hi, lo
                            sel.update(n for n in nums if lo <= n <= hi)
                    else:
                        v = float(part)
                        if v in nums:
                            sel.add(v)
        except Exception:
            pass

        if not sel:
            return await interaction.response.send_message(
                f"❌ لم يُعثر على فصول مطابقة.\nالمتاح: `{_lbl(min(nums))}` ← `{_lbl(max(nums))}`",
                ephemeral=True,
            )

        self.panel.selected = sorted(set(self.panel.selected) | sel, reverse=True)
        self.panel._cap_selection()
        self.panel.page = self.panel._page_for(max(sel))
        self.panel._rebuild(
            f"✓ أُضيف {len(sel)} فصل  ·  Ch.{_lbl(min(sel))} → Ch.{_lbl(max(sel))}"
        )
        await interaction.response.edit_message(
            embed=None,
            view=self.panel,
        )


# ─────────────────────────────────────────────────────────────────────────
#  Modal — إعدادات SmartStitch
# ─────────────────────────────────────────────────────────────────────────
class StitchSettingsModal(ui.Modal, title="إعدادات SmartStitch"):
    width = ui.TextInput(
        label="عرض الصورة (px)",
        placeholder="800",
        default="800",
        min_length=2,
        max_length=5,
        required=True,
    )
    height = ui.TextInput(
        label="الحد الأقصى للارتفاع (px)",
        placeholder="14500",
        default="14500",
        min_length=3,
        max_length=6,
        required=True,
    )
    sensitivity = ui.TextInput(
        label="حساسية الدمج (1-100)",
        placeholder="90",
        default="90",
        min_length=1,
        max_length=3,
        required=True,
    )

    def __init__(self, panel: "MangaPanelView"):
        super().__init__()
        self.panel = panel
        self.width.default = str(panel.stitch_width)
        self.height.default = str(panel.stitch_height)
        self.sensitivity.default = str(panel.stitch_sensitivity)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            w = int(self.width.value.strip())
            h = int(self.height.value.strip())
            sens = int(self.sensitivity.value.strip())

            self.panel.stitch_width = w
            self.panel.stitch_height = h
            self.panel.stitch_sensitivity = sens

            await self.panel._update_msg(interaction, "✓ تم تحديث إعدادات SmartStitch")
        except ValueError:
            await interaction.response.send_message("❌ قيم غير صالحة.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ خطأ: {e}", ephemeral=True)


class MangaPanelView(ui.LayoutView):
    def __init__(
        self,
        bot,
        downloader,
        provider_manager,
        series_url,
        chapters_dict,
        requester: discord.User = None,
        provider_name: str = "Generic",
        cover_url: str = None,
        locked_chapters: set = None,
        default_upload: str = "Auto",
    ):
        super().__init__(timeout=1800)
        self.bot = bot
        self.downloader = downloader
        self.provider_manager = provider_manager
        self.series_url = series_url
        self.requester = requester
        self.provider_name = provider_name
        self.cover_url = cover_url
        self.locked_chapters = locked_chapters or set()

        self.all_chapters: list[float] = sorted(chapters_dict.keys(), reverse=True)
        self.chapters_dict = chapters_dict
        self.page = 0
        self.selected: list[float] = []
        self.ch_status: dict = {}
        self.running = False
        self.cancelled = False

        # ── إعدادات SmartStitch ────────────────────────────────────────────
        self.stitch_enabled: bool = True  # SmartStitch ON/OFF
        self.stitch_width: int = 800
        self.stitch_height: int = 14500
        self.stitch_sensitivity: int = 50

        # ── ترتيب الفصول ──────────────────────────────────────────────────
        self.sort_desc: bool = True  # True = تنازلي (أحدث أولاً)
        self.show_advanced: bool = False  # إظهار الإعدادات المتقدمة
        self.upload_dest: str = (
            default_upload  # الوجهة: Auto, Drive, Gofile, Catbox, Discord
        )
        self._last_batch_dest = None
        self.series_display = series_url

        self._rebuild()

    def _cap_selection(self) -> list[float]:
        """يحدّ الاختيار بـ MAX_SELECT_PER_JOB فصل."""
        sel = sorted(self.selected, reverse=self.sort_desc)
        if len(sel) > MAX_SELECT_PER_JOB:
            sel = sel[:MAX_SELECT_PER_JOB]
        self.selected = sel
        return sel

    async def _ping_chapter_done(self, channel: discord.TextChannel, chapter_lbl: str):
        if not self.requester or not channel:
            return
        try:
            await channel.send(
                f"{self.requester.mention} ✅ **Ch.{chapter_lbl}**",
                delete_after=PING_DELETE_AFTER_SEC,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception:
            pass

    # ── الترتيب ────────────────────────────────────────────────────────────
    def _apply_sort(self):
        self.all_chapters = sorted(
            self.all_chapters,
            reverse=self.sort_desc,
        )

    @property
    def total_pages(self) -> int:
        return max(
            1, (len(self.all_chapters) + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE
        )

    @property
    def page_chs(self) -> list[float]:
        s = self.page * CHAPTERS_PER_PAGE
        return self.all_chapters[s : s + CHAPTERS_PER_PAGE]

    def _page_for(self, num: float) -> int:
        try:
            return self.all_chapters.index(num) // CHAPTERS_PER_PAGE
        except ValueError:
            return 0

    def _completion_dest(self) -> tuple[str | None, str, str]:
        """(dest_key, display_label, logo_url) للرسالة النهائية."""
        key = getattr(self, "_last_batch_dest", None) or _batch_upload_dest(
            self.upload_dest
        )
        if key == "Drive":
            return key, "Google Drive", LOGO_DRIVE
        if key == "Gofile":
            return key, "Gofile", LOGO_GOFILE
        if self.upload_dest == "Drive":
            return "Drive", "Google Drive", LOGO_DRIVE
        if self.upload_dest == "Gofile":
            return "Gofile", "Gofile", LOGO_GOFILE
        return None, self.upload_dest, self.bot.user.display_avatar.url

    async def _update_msg(
        self, interaction: discord.Interaction, note: str = None, color=None
    ):
        self._rebuild(note, color)
        await interaction.response.edit_message(embed=None, view=self)

    # ── بناء العناصر ──────────────────────────────────────────────────────
    def _rebuild(self, note: str = None, color=None):
        self.clear_items()
        chs = self.page_chs
        sel_s = set(self.selected)

        if color is None:
            if self.running:
                color = C_RUN
            elif any(v.get("state") == "failed" for v in self.ch_status.values()):
                color = C_FAIL
            elif any(v.get("state") == "done" for v in self.ch_status.values()):
                color = C_DONE
            else:
                color = C_PANEL

        container = discord.ui.Container(accent_color=color)

        series = _series_name(self.series_url)
        site = _domain(self.series_url)
        selcnt = len(self.selected)
        total = len(self.all_chapters)
        mode_txt = "SmartStitch" if self.stitch_enabled else "ZIP"

        title_text = (
            f"# 📖 {series}\n`{site}` · **{mode_txt}** · Upload `{self.upload_dest}`"
        )
        container.add_item(discord.ui.TextDisplay(title_text))
        add_cover_media(container, self.cover_url)

        if not self.running:
            desc_val = (
                f"Choose up to **{MAX_SELECT_PER_JOB}** chapters per request.\n"
                f"**Select Range** → e.g. `1-3, 10, 15-16` (chapters, from list below)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"**Selected:** `{selcnt}` / `{total}`  ·  "
                f"**Mode:** `{mode_txt}`  ·  **Upload:** `{self.upload_dest}`"
            )
        else:
            desc_val = (
                f"**Processing…**  ·  `{site}`\n"
                f"**Selected:** `{selcnt}` / `{total}`  ·  "
                f"**Mode:** `{mode_txt}`  ·  **Upload:** `{self.upload_dest}`"
            )
        container.add_item(discord.ui.TextDisplay(desc_val))

        # Chapter List
        rows = []
        for n in chs:
            is_locked = n in self.locked_chapters
            in_sel = n in sel_s
            state = self.ch_status.get(n, {}).get("state", "")

            if state in ICO:
                ico = ICO[state]
            elif in_sel:
                ico = "🟦"
            elif is_locked:
                ico = "🔒"
            else:
                ico = "◻️"

            # Match actual chapter number
            mark = "✓" if in_sel else " "
            lock_sfx = " 🔒" if is_locked else ""
            rows.append(f"`[{mark}]` **{_lbl(n):>4}.** Chapter {_lbl(n)}{lock_sfx}")

        if rows:
            block = "\n".join(rows)
            if len(block) > 1000:
                block = block[:997] + "…"
            container.add_item(
                discord.ui.Separator(
                    visible=True, spacing=discord.SeparatorSpacing.small
                )
            )
            container.add_item(
                discord.ui.TextDisplay(
                    f"**Chapters — Page {self.page + 1} / {self.total_pages}**\n{block}"
                )
            )

        if self.selected:
            sel_nums = sorted(self.selected)
            done_ready = [
                n for n in sel_nums if self.ch_status.get(n, {}).get("state") == "done"
            ]
            failed = [
                n
                for n in sel_nums
                if self.ch_status.get(n, {}).get("state") == "failed"
            ]
            pending = [
                n
                for n in sel_nums
                if self.ch_status.get(n, {}).get("state") not in ("done", "failed")
            ]
            summary_range = (
                f"Ch.**{_lbl(min(sel_nums))}** → Ch.**{_lbl(max(sel_nums))}**"
                if sel_nums
                else "—"
            )

            parts = [f"📌 **{selcnt}** chapters"]
            if done_ready:
                parts.append(f"✅ {len(done_ready)} done")
            if pending:
                parts.append(f"⏳ {len(pending)} pending")
            if failed:
                parts.append(f"❌ {len(failed)} failed")

            container.add_item(
                discord.ui.TextDisplay(
                    f"**🎯 Selection Stats**\n{' · '.join(parts)}\n{summary_range}"
                )
            )

        if self.stitch_enabled and self.show_advanced:
            container.add_item(
                discord.ui.TextDisplay(
                    f"**🧵 Stitch Settings**\nWidth: `{self.stitch_width}px` | Sensitivity: `{self.stitch_sensitivity}%`"
                )
            )

        if self.running:
            running_ = [
                n
                for n in self.selected
                if self.ch_status.get(n, {}).get("state")
                in ("downloading", "stitching", "uploading")
            ]
            queued_ = [
                n
                for n in self.selected
                if self.ch_status.get(n, {}).get("state") == "queued"
            ]
            done_n = sum(
                1
                for n in self.selected
                if self.ch_status.get(n, {}).get("state") == "done"
            )
            fail_n = sum(
                1
                for n in self.selected
                if self.ch_status.get(n, {}).get("state") == "failed"
            )
            total_dl = len(self.selected)
            done_pct = int(done_n * 100 / total_dl) if total_dl else 0

            progress_bar = pbar(done_pct, 14)
            container.add_item(
                discord.ui.TextDisplay(
                    f"**📊 Overall Progress**\n`{progress_bar}`\n"
                    f"⚡ **{len(running_)}** active  ·  ⏳ {len(queued_)} queued  "
                    f"·  ✅ {done_n}  ·  ❌ {fail_n}"
                )
            )

            active_lines = []
            other_lines = []
            for n in sorted(self.selected):
                st = self.ch_status.get(n, {})
                state = st.get("state", "queued")
                pct = st.get("progress", 0)
                lbl_n = _lbl(n)

                if state == "done":
                    line = f"✅ Ch.{lbl_n} Done"
                elif state == "failed":
                    line = f"❌ Ch.{lbl_n} Failed"
                elif state == "stitching":
                    line = f"🧵 Ch.{lbl_n} Stitching {pct}%"
                elif state == "uploading":
                    line = f"📤 Ch.{lbl_n} Uploading {pct}%"
                elif state == "downloading":
                    line = f"📥 Ch.{lbl_n} Downloading {pct}%"
                else:
                    line = f"⏳ Ch.{lbl_n} Queued"

                if state in ("downloading", "stitching", "uploading"):
                    active_lines.append(line)
                else:
                    other_lines.append(line)

            display_lines = active_lines + other_lines
            grid = "```\n"
            for ln in display_lines[:15]:
                grid += ln + "\n"
            if len(self.selected) > 15:
                grid += f"... +{len(self.selected) - 15} chapters in queue\n"
            grid += "```"
            container.add_item(
                discord.ui.TextDisplay(f"**📊 Active Downloads & Queue**\n{grid}")
            )

        ready = [
            (n, self.ch_status[n])
            for n in sorted(self.selected)
            if self.ch_status.get(n, {}).get("state") == "done"
            and self.ch_status[n].get("link")
        ]
        if ready and not self.running:
            linked = [(n, d) for n, d in ready if d.get("link")]
            if linked:
                lnks = "\n".join(
                    f"📎 **[Ch.{_lbl(n)}]({d['link']})** — `{d.get('provider', '')}`"
                    for n, d in linked[:10]
                )
                if len(linked) > 10:
                    lnks += f"\n*… +{len(linked) - 10} more*"
                container.add_item(
                    discord.ui.TextDisplay(f"**🔗 Download Links**\n{lnks}")
                )

        if note:
            container.add_item(
                discord.ui.TextDisplay(f"💬 **Status**:\n```fix\n{note[:200]}\n```")
            )

        # Build interactive elements
        if chs:
            opts = []
            for n in chs:
                st = self.ch_status.get(n, {})
                state = st.get("state", "")
                is_done = state == "done"
                in_sel = n in sel_s
                is_locked = n in self.locked_chapters

                emoji = ICO.get(
                    state,
                    ICO["selected"]
                    if in_sel
                    else (ICO["locked"] if is_locked else ICO["idle"]),
                )
                desc = f"Ch. {_lbl(n)}"
                if is_done:
                    desc += " (Ready)"
                elif in_sel:
                    desc += " (Selected)"
                elif is_locked:
                    desc += " (Paid/Locked)"

                opts.append(
                    discord.SelectOption(
                        label=f"Chapter {_lbl(n)}",
                        value=str(n),
                        emoji=emoji,
                        description=desc,
                        default=in_sel,
                    )
                )
            menu = discord.ui.Select(
                placeholder=f"Select chapters · Page {self.page + 1}/{self.total_pages}",
                min_values=0,
                max_values=len(opts),
                options=opts,
                disabled=self.running,
            )
            menu.callback = self._cb_select
            add_select_row(container, menu)

        # Nav action row
        nav_row = discord.ui.ActionRow()
        d = self.running

        b_first = discord.ui.Button(
            emoji="⏮️", style=discord.ButtonStyle.secondary, disabled=self.page == 0 or d
        )
        b_first.callback = self._cb_first
        nav_row.add_item(b_first)

        b_prev = discord.ui.Button(
            emoji="◀️", style=discord.ButtonStyle.secondary, disabled=self.page == 0 or d
        )
        b_prev.callback = self._cb_prev
        nav_row.add_item(b_prev)

        b_page = discord.ui.Button(
            label=f"{self.page + 1} / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        nav_row.add_item(b_page)

        b_next = discord.ui.Button(
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1 or d,
        )
        b_next.callback = self._cb_next
        nav_row.add_item(b_next)

        b_last = discord.ui.Button(
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1 or d,
        )
        b_last.callback = self._cb_last
        nav_row.add_item(b_last)
        container.add_item(nav_row)

        row2 = discord.ui.ActionRow()
        if self.running:
            b_stop = discord.ui.Button(
                label="Stop", emoji="🛑", style=discord.ButtonStyle.danger
            )
            b_stop.callback = self._cb_stop
            row2.add_item(b_stop)
        else:
            b_range = discord.ui.Button(
                label="Select Range", style=discord.ButtonStyle.primary
            )
            b_range.callback = self._cb_range
            row2.add_item(b_range)

            b_all = discord.ui.Button(
                label="Select All", style=discord.ButtonStyle.success
            )
            b_all.callback = self._cb_all
            row2.add_item(b_all)

            b_clear = discord.ui.Button(
                label="Clear All", style=discord.ButtonStyle.danger
            )
            b_clear.callback = self._cb_clear
            row2.add_item(b_clear)
        container.add_item(row2)

        if not self.running:
            row3 = discord.ui.ActionRow()
            n_sel = len(self.selected)
            b_confirm = discord.ui.Button(
                label=f"Confirm ({n_sel})" if n_sel else "Confirm (0)",
                style=discord.ButtonStyle.success,
                disabled=not n_sel,
            )
            b_confirm.callback = self._cb_start
            row3.add_item(b_confirm)

            b_close = discord.ui.Button(
                label="Close", emoji="✖️", style=discord.ButtonStyle.secondary
            )
            b_close.callback = self._cb_close
            row3.add_item(b_close)

            if any(v.get("state") == "failed" for v in self.ch_status.values()):
                b_retry = discord.ui.Button(
                    label="Retry", emoji="🔄", style=discord.ButtonStyle.secondary
                )
                b_retry.callback = self._cb_retry
                row3.add_item(b_retry)
            container.add_item(row3)

            if self.show_advanced:
                row4 = discord.ui.ActionRow()
                b_mode = discord.ui.Button(
                    label="Stitch" if self.stitch_enabled else "ZIP",
                    emoji="🧵" if self.stitch_enabled else "📦",
                    style=discord.ButtonStyle.secondary,
                )
                b_mode.callback = self._cb_mode
                row4.add_item(b_mode)

                b_sett = discord.ui.Button(
                    label="Config",
                    emoji="⚙️",
                    style=discord.ButtonStyle.secondary,
                    disabled=not self.stitch_enabled,
                )
                b_sett.callback = self._cb_settings
                row4.add_item(b_sett)

                dest_icons = {
                    "Auto": "🤖",
                    "Drive": "📂",
                    "Gofile": "☁️",
                    "Catbox": "📦",
                    "Discord": "💬",
                }
                b_dest = discord.ui.Button(
                    label=self.upload_dest,
                    emoji=dest_icons.get(self.upload_dest, "🌐"),
                    style=discord.ButtonStyle.secondary,
                )
                b_dest.callback = self._cb_upload_dest
                row4.add_item(b_dest)

                b_sort = discord.ui.Button(
                    label="↓ New" if self.sort_desc else "↑ Old",
                    emoji="🔃",
                    style=discord.ButtonStyle.secondary,
                )
                b_sort.callback = self._cb_sort
                row4.add_item(b_sort)
                container.add_item(row4)

            row5 = discord.ui.ActionRow()
            b_advanced = discord.ui.Button(
                label="Hide Advanced" if self.show_advanced else "Advanced",
                emoji="⚙️",
                style=discord.ButtonStyle.secondary,
            )
            b_advanced.callback = self._cb_toggle_advanced
            row5.add_item(b_advanced)
            container.add_item(row5)

        self.add_item(container)

    # ── navigation callbacks ──────────────────────────────────────────────
    async def _cb_select(self, interaction: discord.Interaction):
        chosen = {float(v) for v in interaction.data["values"]}
        page_s = set(self.page_chs)
        others = {n for n in self.selected if n not in page_s}
        self.selected = sorted(others | chosen, reverse=self.sort_desc)
        self._cap_selection()
        await self._update_msg(
            interaction, f"☑ Selected: {len(self.selected)} chapter(s)"
        )

    async def _cb_first(self, i):
        self.page = 0
        await self._update_msg(i)

    async def _cb_prev(self, i):
        self.page = max(0, self.page - 1)
        await self._update_msg(i)

    async def _cb_next(self, i):
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._update_msg(i)

    async def _cb_last(self, i):
        self.page = self.total_pages - 1
        await self._update_msg(i)

    # ── quick-select ──────────────────────────────────────────────────────
    async def _cb_l1(self, i):
        self.selected = self.all_chapters[:1]
        await self._update_msg(i, f"⭐  آخر فصل  ─  Ch.{_lbl(self.selected[0])}")

    async def _cb_l5(self, i):
        self.selected = list(self.all_chapters[:5])
        await self._update_msg(i, "📦  آخر 5 فصول")

    async def _cb_l10(self, i):
        self.selected = list(self.all_chapters[:10])
        await self._update_msg(i, "🔟  آخر 10 فصول")

    async def _cb_pg(self, i):
        pg = set(self.page_chs)
        oth = {n for n in self.selected if n not in pg}
        self.selected = sorted(oth | pg, reverse=self.sort_desc)
        await self._update_msg(i, f"📄  أُضيفت كل فصول الصفحة ({len(self.page_chs)})")

    async def _cb_sort(self, i):
        self.sort_desc = not self.sort_desc
        self._apply_sort()
        # إعادة ترتيب المحدود بنفس الاتجاه
        self.selected = sorted(self.selected, reverse=self.sort_desc)
        self.page = 0
        lbl = "تنازلي (أحدث أولاً)" if self.sort_desc else "تصاعدي (أقدم أولاً)"
        await self._update_msg(i, f"🔀  الترتيب: {lbl}")

    async def _cb_all(self, i):
        """تحديد أحدث الفصول (حتى الحد الأقصى)."""
        self.selected = list(self.all_chapters[:MAX_SELECT_PER_JOB])
        await self._update_msg(i, f"✅ تم تحديد جميع الفصول ({len(self.selected)})")

    async def _cb_range(self, i):
        await i.response.send_modal(RangeModal(self))

    async def _cb_settings(self, i):
        if not self.stitch_enabled:
            return await i.response.send_message(
                "⚠️  تفعّل وضع SmartStitch أولاً لتغيير الإعدادات.", ephemeral=True
            )
        await i.response.send_modal(StitchSettingsModal(self))

    async def _cb_mode(self, i):
        self.stitch_enabled = not self.stitch_enabled
        mode = "SmartStitch ✓" if self.stitch_enabled else "ZIP فقط (بلا دمج)"
        await self._update_msg(i, f"⚡  الوضع: {mode}")

    async def _cb_upload_dest(self, i):
        """التنقل بين وجهات الرفع."""
        dests = ["Auto", "Drive", "Gofile", "Catbox", "Discord"]
        idx = dests.index(self.upload_dest)
        self.upload_dest = dests[(idx + 1) % len(dests)]
        await self._update_msg(i, f"🎯 وجهة الرفع المحددة: {self.upload_dest}")

    async def _cb_clear(self, i):
        self.selected = []
        self.ch_status = {}
        await self._update_msg(i, "🗑  مُسح الاختيار")

    async def _cb_toggle_advanced(self, i):
        self.show_advanced = not self.show_advanced
        await self._update_msg(i)

    async def _cb_close(self, i):
        self.clear_items()
        container = discord.ui.Container(accent_color=C_GREY)
        container.add_item(discord.ui.TextDisplay("✖  اللوحة مغلقة"))
        self.add_item(container)
        await i.response.edit_message(embed=None, view=self)
        self.stop()

    async def _cb_retry(self, i):
        """إعادة المحاولة للفصول الفاشلة."""
        failed = [n for n, v in self.ch_status.items() if v.get("state") == "failed"]
        if not failed:
            return await i.response.send_message("لا توجد فصول فاشلة.", ephemeral=True)
        # أعد تعيين الفاشلة لـ queued
        for n in failed:
            self.ch_status[n] = {"state": "queued"}
        # ضعها في المحدد
        self.selected = sorted(set(self.selected) | set(failed), reverse=self.sort_desc)
        await self._update_msg(i, f"🔄  إعادة {len(failed)} فصل فاشل...")
        # ابدأ التحميل في الخلفية
        self.cancelled = False
        asyncio.create_task(self._run_downloads(i.message, failed))

    # ── start download ─────────────────────────────────────────────────────
    async def _cb_start(self, interaction: discord.Interaction):
        if self.running:
            return await interaction.response.send_message(
                "⚠️ عملية جارية.", ephemeral=True
            )
        if not self.selected:
            return await interaction.response.send_message(
                "❗ اختر فصولاً أولاً.", ephemeral=True
            )

        to_dl = self._cap_selection()
        if len(to_dl) > MAX_SELECT_PER_JOB:
            return await interaction.response.send_message(
                f"❌ Maximum **{MAX_SELECT_PER_JOB}** chapters per request.",
                ephemeral=True,
            )
        to_dl = sorted(to_dl, reverse=self.sort_desc)
        for n in to_dl:
            if self.ch_status.get(n, {}).get("state") != "done":
                self.ch_status[n] = {"state": "queued"}

        self.running = True
        await self._update_msg(
            interaction,
            f"🚀 بدء تحميل {len(to_dl)} فصل  ·  توازي×{DL_CONCURRENT}",
            color=C_RUN,
        )

        self.cancelled = False
        # تشغيل التحميل في الخلفية حتى ينتهي الكولباك فوراً ولا يعلق الديسكورد
        asyncio.create_task(self._run_downloads(interaction.message, to_dl))

    async def _cb_stop(self, i):
        self.cancelled = True
        self.running = False
        await self._update_msg(i, "🛑 جاري الإيقاف...")

    # ── منطق التحميل المتوازي ─────────────────────────────────────────────
    async def _run_downloads(self, panel_msg: discord.Message, to_dl: list):
        try:
            self.running = True
            done_list = []
            fail_list = []
            last_edit_ts = 0.0
            edit_lock = asyncio.Lock()

            # ── إنشاء مجلد مجمع إذا كان العدد أكبر من 1 ──────────────────────
            batch_folder_id = None
            batch_folder_link = None
            batch_dest = (
                _batch_upload_dest(self.upload_dest) if len(to_dl) > 1 else None
            )
            # تسلسل الفصول داخل المجلد الواحد لتجنب تداخل الرفع على Gofile/Drive
            sem = asyncio.Semaphore(
                1 if batch_dest and len(to_dl) > 1 else DL_CONCURRENT
            )

            if len(to_dl) > 1 and batch_dest:
                s_name = _series_name(self.series_url)
                import random

                ts = datetime.datetime.now().strftime("%m%d_%H%M")
                salt = random.randint(100, 999)
                f_name = (
                    f"{s_name} Ch.{_lbl(min(to_dl))}-{_lbl(max(to_dl))} [{ts}_{salt}]"
                )
                remote_down = getattr(self.bot, "remote_down", None)

                if remote_down and remote_down.is_enabled:
                    self._rebuild(
                        f"☁️ جاري إنشاء مجلد {batch_dest} عبر العامل (Worker)..."
                    )
                    await panel_msg.edit(embed=None, view=self)
                    res = await remote_down.create_remote_folder(f_name, batch_dest)
                    if res.get("ok"):
                        batch_folder_id = res["folder_id"]
                        batch_folder_link = res["link"]
                        print(
                            f"✅ Created Remote Folder ({batch_dest}): {batch_folder_id}"
                        )
                    else:
                        raise Exception(
                            f"فشل إنشاء المجلد عبر العامل: {res.get('error')}"
                        )
                elif batch_dest == "Drive":
                    self._rebuild(f"📂 جاري إنشاء مجلد Google Drive:\n`{f_name}`")
                    await panel_msg.edit(embed=None, view=self)
                    f_info = await self.downloader.create_gdrive_folder(f_name)
                    if f_info:
                        batch_folder_id = f_info["id"]
                        batch_folder_link = f_info.get("webViewLink")
                        print(f"✅ Created Drive Folder: {batch_folder_id}")
                    else:
                        raise Exception(
                            "فشل إنشاء مجلد Google Drive. تأكد من GOOGLE_SERVICE_ACCOUNT_JSON"
                        )
                elif batch_dest == "Gofile":
                    self._rebuild(f"☁️ جاري إنشاء مجلد Gofile:\n`{f_name}`")
                    await panel_msg.edit(embed=None, view=self)
                    f_info = await self.downloader.create_gofile_folder(f_name)
                    if f_info:
                        batch_folder_id = f_info["id"]
                        batch_folder_link = f"https://gofile.io/d/{f_info['code']}"
                        print(
                            f"✅ Created Gofile Folder: {batch_folder_id} → {batch_folder_link}"
                        )
                    else:
                        raise Exception(
                            "فشل إنشاء مجلد Gofile. تأكد من صلاحية GOFILE_TOKEN"
                        )

            async def _safe_edit(note=None, col=None):
                nonlocal last_edit_ts
                async with edit_lock:
                    now = asyncio.get_running_loop().time()
                    if col is None and now - last_edit_ts < 2.0:
                        return
                    last_edit_ts = now
                    try:
                        self._rebuild(note, col)
                        await panel_msg.edit(embed=None, view=self)
                    except Exception:
                        pass

            async def _dl_one(num: float):
                url = self.chapters_dict[num]
                lbl = _lbl(num)
                ch_name = _batch_chapter_filename(num)

                async def pcb(cur, tot, txt, _n=num, _l=lbl):
                    if self.cancelled:
                        return
                    pct = min(100, int(cur * 100 / max(tot, 1)))
                    state = "downloading"
                    if any(k in txt for k in ("SmartStitch", "دمج", "🪡", "stitch")):
                        state = "stitching"
                    if "رفع" in txt or "upload" in txt.lower() or "☁️" in txt:
                        state = "uploading"
                    prov = (
                        "Gofile"
                        if "Gofile" in txt
                        else "Catbox"
                        if "Catbox" in txt
                        else ""
                    )
                    self.ch_status[_n].update(
                        {"state": state, "progress": pct, "provider": prov}
                    )
                    await _safe_edit(f"Ch.{_l}  {txt}")

                res = None
                async with sem:
                    wait_msg = f"⏳ Ch.{lbl}  في الانتظار..."
                    self.ch_status[num].update(
                        {"state": "queued", "detail": "Waiting in Global Queue..."}
                    )
                    await _safe_edit(wait_msg)

                    async with GLOBAL_SEMAPHORE:
                        try:
                            if self.cancelled:
                                self.ch_status[num] = {
                                    "state": "failed",
                                    "detail": "Cancelled by user",
                                }
                                return
                            if self.ch_status.get(num, {}).get("state") == "done":
                                return

                            remote_down = getattr(self.bot, "remote_down", None)
                            worker_dest = batch_dest or self.upload_dest
                            if remote_down and remote_down.is_enabled:
                                self.ch_status[num].update(
                                    {"state": "downloading", "provider": "HF Worker"}
                                )
                                await _safe_edit(f"🖥️ Ch.{lbl}  إرسال إلى HF Worker...")
                                job = await remote_down.start_download(
                                    url,
                                    ch_name,
                                    params={
                                        "folder_id": batch_folder_id,
                                        "upload_dest": worker_dest,
                                    },
                                )
                                if "error" in job:
                                    raise Exception(f"Worker Error: {job['error']}")
                                result = await remote_down.wait_for_job(
                                    job["job_id"], progress_callback=pcb
                                )
                                if result.get("status") == "completed":
                                    res_link = batch_folder_link or result.get("result")
                                    prov = (
                                        "Google Drive"
                                        if batch_dest == "Drive"
                                        else (
                                            "Gofile"
                                            if batch_dest == "Gofile"
                                            else "HF Space"
                                        )
                                    )
                                    self.ch_status[num] = {
                                        "state": "done",
                                        "progress": 100,
                                        "provider": prov,
                                        "link": res_link
                                        if not batch_folder_link
                                        else None,
                                    }
                                    await _safe_edit(
                                        f"✓ Ch.{lbl}  (عبر HF)", col=C_DONE
                                    )
                                    return
                                raise Exception(
                                    result.get("message", "Worker task failed")
                                )

                            upload_dest = batch_dest or self.upload_dest

                            if self.stitch_enabled:
                                res = await self.downloader.download_and_stitch(
                                    url,
                                    ch_name,
                                    target_height=self.stitch_height,
                                    target_width=self.stitch_width,
                                    sensitivity=self.stitch_sensitivity,
                                    progress_callback=pcb,
                                    upload_dest=upload_dest,
                                    folder_id=batch_folder_id,
                                )
                            else:
                                zip_fp = await self.downloader.download_chapter(
                                    url, ch_name, progress_callback=pcb
                                )
                                if zip_fp:
                                    if batch_folder_id and batch_dest == "Drive":
                                        link = await self.downloader.upload_to_gdrive(
                                            zip_fp,
                                            f"{ch_name}.zip",
                                            parent_folder_id=batch_folder_id,
                                        )
                                        if link:
                                            res = {"link": link, "type": "drive_folder"}
                                    elif batch_folder_id and batch_dest == "Gofile":
                                        link = await self.downloader.upload_to_gofile(
                                            zip_fp,
                                            folder_id=batch_folder_id,
                                            remote_filename=f"{ch_name}.zip",
                                        )
                                        if link:
                                            res = {"link": link, "type": "gofile"}
                                    else:
                                        res = {"link": zip_fp, "type": "local_zip"}

                            if not res:
                                self.ch_status[num] = {
                                    "state": "failed",
                                    "detail": "فشل جلب الصور",
                                }
                                await _safe_edit(f"✗ Ch.{lbl}  فشل التحميل", col=C_FAIL)
                                return

                            fp, res_type = res.get("link"), res.get("type")
                            if res_type in ("gofile", "drive_folder", "catbox"):
                                prov = res_type.replace("_", " ").title()
                                if prov == "Drive Folder":
                                    prov = "Google Drive"
                                chapter_link = None if batch_folder_link else fp
                                self.ch_status[num] = {
                                    "state": "done",
                                    "progress": 100,
                                    "provider": prov,
                                    "link": chapter_link,
                                }
                                await _safe_edit(f"✓ Ch.{lbl}  {prov}", col=C_DONE)
                                return
                            if res_type == "local_zip":
                                if upload_dest == "Discord":
                                    self.ch_status[num] = {
                                        "state": "done",
                                        "progress": 100,
                                        "provider": "Local ZIP",
                                        "link": fp,
                                    }
                                    await _safe_edit(
                                        f"✓ Ch.{lbl}  Local ZIP", col=C_DONE
                                    )
                                else:
                                    self.ch_status[num] = {
                                        "state": "failed",
                                        "detail": "فشل الرفع للسيرفرات",
                                    }
                                    await _safe_edit(
                                        f"✗ Ch.{lbl}  فشل الرفع", col=C_FAIL
                                    )
                        except Exception as e:
                            self.ch_status[num] = {
                                "state": "failed",
                                "detail": str(e)[:80],
                            }
                            await _safe_edit(f"✗ Ch.{lbl}  خطأ: {e}", col=C_FAIL)
                        finally:
                            if (
                                res
                                and res.get("type") == "local_zip"
                                and res.get("link")
                            ):
                                self.downloader.cleanup(res["link"])

            await asyncio.gather(*[_dl_one(n) for n in to_dl])

            # ── منشن واحد لكل الفصول المكتملة ─────────────────────────────────
            done_chs = [
                (n, s) for n, s in self.ch_status.items()
                if n in to_dl and s.get("state") == "done"
            ]
            if self.requester and done_chs and panel_msg:
                try:
                    done_lbls = [_lbl(n) for n, _ in done_chs]
                    if len(done_lbls) <= 5:
                        label = ", ".join(done_lbls)
                    else:
                        label = f"{len(done_lbls)} فصول ({done_lbls[0]}–{done_lbls[-1]})"
                    await panel_msg.channel.send(
                        f"{self.requester.mention} ✅ تم تحميل **{label}**",
                        delete_after=PING_DELETE_AFTER_SEC,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except Exception:
                    pass

            # ── انتهى التحميل ──────────────────────────────────────────────────
            self.running = False
            done_list = [
                (n, s)
                for n, s in self.ch_status.items()
                if n in to_dl and s.get("state") == "done"
            ]
            fail_list = [
                (n, s)
                for n, s in self.ch_status.items()
                if n in to_dl and s.get("state") == "failed"
            ]

            main_link = batch_folder_link
            if not main_link and done_list:
                for _, data in done_list:
                    if data.get("link"):
                        main_link = data["link"]
                        break

            multi_folder = bool(batch_folder_link and len(to_dl) > 1)
            self._last_batch_dest = batch_dest
            try:
                self.clear_items()
                dest_key, dest_label, logo = self._completion_dest()

                if fail_list and not done_list:
                    color = C_FAIL
                elif fail_list:
                    color = C_RUN
                else:
                    color = C_DONE

                series = _series_name(self.series_url)
                num_done = len(done_list)
                if num_done > 1:
                    nums = sorted(n for n, _ in done_list)
                    chapter_line = format_chapter_line(
                        count=num_done,
                        start=_lbl(nums[0]),
                        end=_lbl(nums[-1]),
                    )
                elif num_done == 1:
                    chapter_line = format_chapter_line(single=_lbl(done_list[0][0]))
                else:
                    chapter_line = "No chapters were successfully downloaded."

                failed_line = None
                if fail_list:
                    failed_nums = ", ".join(_lbl(n) for n, _ in fail_list[:8])
                    extra = (
                        f" (+{len(fail_list) - 8} more)" if len(fail_list) > 8 else ""
                    )
                    failed_line = f"**Failed chapters**\n`{failed_nums}`{extra}"

                container = build_download_completed_container(
                    series_name=series,
                    series_url=self.series_url,
                    chapter_line=chapter_line,
                    main_link=main_link if dest_key in ("Drive", "Gofile") else None,
                    provider=dest_label,
                    cover_url=self.cover_url,
                    multi_folder=multi_folder,
                    failed_line=failed_line,
                    color=color,
                )

                row = discord.ui.ActionRow()
                if fail_list:
                    b_retry = discord.ui.Button(
                        label=f"Retry {len(fail_list)} Failed",
                        emoji="🔄",
                        style=discord.ButtonStyle.danger,
                    )
                    b_retry.callback = self._cb_retry
                    row.add_item(b_retry)

                if len(row.children) > 0:
                    container.add_item(row)

                self.add_item(container)
                await panel_msg.edit(embed=None, view=self)
            except Exception as ui_err:
                print(f"[Panel] completion UI edit failed: {ui_err}")

        except Exception as e:
            done_count = sum(
                1 for n in to_dl if self.ch_status.get(n, {}).get("state") == "done"
            )
            print(f"[_run_downloads] error (done={done_count}): {e}")
            self.running = False
            if done_count == 0:
                try:
                    self.clear_items()
                    container = discord.ui.Container(accent_color=C_FAIL)
                    container.add_item(
                        discord.ui.TextDisplay(
                            f"# Download failed\n```{str(e)[:300]}```"
                        )
                    )
                    self.add_item(container)
                    await panel_msg.edit(embed=None, view=self)
                except Exception:
                    pass
        finally:
            self.running = False
            self._rebuild()


from providers.manager import ProviderManager


# ─────────────────────────────────────────────────────────────────────────
#  RadarCog
# ─────────────────────────────────────────────────────────────────────────
class RadarCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.downloader = MangaDownloader()
        self.provider_manager = ProviderManager()
        self.tracker_card_view = TrackerCardV2View()
        self._consecutive_failures = {}
        self.adaptive_poller = AdaptivePoller()
        self.feed_watcher = FeedWatcher(self.provider_manager)
        self._active_downloads: set[tuple[int, float]] = set()
        self._recently_notified: dict[tuple[int, float], datetime.datetime] = {}
        self._status_index = 0
        self.autodl_semaphore = asyncio.Semaphore(2)

        self.chapter_radar_loop.start()
        self.global_feed_loop.start()
        self.reminder_loop.start()
        self.locked_cards_updater_loop.start()
        self.presence_updater_loop.start()

    async def _get_or_fetch_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(channel_id)
        if not ch:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                print(f"[Radar] Failed to fetch channel {channel_id}: {e}")
                ch = None
        return ch

    async def cog_load(self) -> None:
        # Persistent view for Tracker Card v2 (buttons remain after restart)
        self.bot.add_view(self.tracker_card_view)

    def cog_unload(self):
        self.chapter_radar_loop.cancel()
        self.global_feed_loop.cancel()
        self.reminder_loop.cancel()
        self.locked_cards_updater_loop.cancel()
        self.presence_updater_loop.cancel()

    async def fetch_latest(self, url: str, cur: float) -> Optional[float]:
        try:
            latest = await self.provider_manager.get_latest_chapter(url)
            if latest and latest > cur and latest <= cur + 15:
                return latest
        except Exception as e:
            print(f"[Radar] {url}: {e}")
        return None

    @tasks.loop(seconds=30)
    async def chapter_radar_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now(datetime.timezone.utc)
        trackers = await database.get_all_trackers()
        if not trackers:
            return

        mapped_trackers = []
        for r in trackers:
            priority = r[13] if len(r) > 13 else "normal"
            heat_score = r[14] if len(r) > 14 else 50.0
            last_release_at = r[15] if len(r) > 15 else None
            release_pattern = r[16] if len(r) > 16 else None
            check_method = r[17] if len(r) > 17 else "scrape"
            consecutive_failures = r[18] if len(r) > 18 else 0

            mapped_trackers.append({
                "tracker_id": r[0],
                "guild_id": r[1],
                "channel_id": r[2],
                "url": r[3],
                "last_chapter": r[4],
                "custom_msg": r[5],
                "interval_hours": r[6],
                "last_checked": r[7],
                "download_enabled": r[8],
                "paused": r[9],
                "title": r[10],
                "mention_str": r[11],
                "interval_minutes": r[12],
                "priority": priority or "normal",
                "heat_score": heat_score if heat_score is not None else 50.0,
                "last_release_at": last_release_at,
                "release_pattern": release_pattern,
                "check_method": check_method or "scrape",
                "consecutive_failures": consecutive_failures if consecutive_failures is not None else 0
            })

        due = await self.adaptive_poller.get_due_trackers(mapped_trackers, now)
        if not due:
            return

        print(f"[Radar] Checking {len(due)} trackers via Adaptive Poller...")
        sem = asyncio.Semaphore(RADAR_CONCURRENT)

        async def check_one(tr_dict):
            tid = tr_dict["tracker_id"]
            gid = tr_dict["guild_id"]
            cid = tr_dict["channel_id"]
            url = tr_dict["url"]
            last_ch = tr_dict["last_chapter"]
            custom_msg = tr_dict["custom_msg"]
            dl_en = tr_dict["download_enabled"]
            title = tr_dict["title"]
            mention_str = tr_dict["mention_str"]
            priority = tr_dict["priority"]
            heat_score = tr_dict["heat_score"]
            check_method = tr_dict["check_method"]
            consecutive_failures = tr_dict["consecutive_failures"]
            release_pattern = tr_dict["release_pattern"]

            async with sem:
                await asyncio.sleep(random.uniform(2.0, 5.0))
                try:
                    # Auto-detect check method if it is scrape and can be feed/api based
                    detected_method = self.feed_watcher.detect_check_method(url)
                    if detected_method != check_method and check_method == "scrape":
                        check_method = detected_method

                    quick_res = await self.feed_watcher.quick_check_individual(url, last_ch, check_method)
                    
                    latest = None
                    new_items: list[dict] = []
                    
                    if quick_res:
                        latest = quick_res["latest"]
                        new_items = quick_res["new_chapters"]
                    else:
                        remote_down = getattr(self.bot, "remote_down", None)
                        if remote_down and getattr(remote_down, "is_enabled", False):
                            res = await remote_down.radar_check(url, last_chapter=last_ch, max_new=25)
                            if res and res.get("ok"):
                                latest = float(res.get("latest") or 0)
                                new_items = list(res.get("new_chapters") or [])
                        else:
                            rich = await self.provider_manager.get_chapters_with_lock_info(url)
                            if rich:
                                latest = float(max(rich.keys()))
                                new_items = []
                                for n in sorted([k for k in rich.keys() if k > last_ch]):
                                    info = rich.get(n)
                                    if isinstance(info, dict):
                                        new_items.append({
                                            "num": float(n),
                                            "url": info.get("url", ""),
                                            "locked": bool(info.get("locked")),
                                        })
                                    else:
                                        new_items.append({
                                            "num": float(n),
                                            "url": str(info or ""),
                                            "locked": False,
                                        })

                    if latest is None or latest <= last_ch or not new_items:
                        decayed_heat = self.adaptive_poller.update_heat(heat_score, "no_change")
                        await database.update_tracker_time(tid, now.isoformat())
                        await database.update_tracker_polling_state(tid, decayed_heat, priority, check_method, 0)
                        return

                    # Deduplicate notifications
                    key = (tid, float(latest))
                    now_time = datetime.datetime.now(datetime.timezone.utc)
                    self._recently_notified = {k: v for k, v in self._recently_notified.items() if (now_time - v).total_seconds() < 600}
                    if key in self._recently_notified:
                        print(f"[Radar Poller] ⚠️ Alert already sent recently for {title} Ch.{latest}. Skipping duplicate.")
                        return
                    self._recently_notified[key] = now_time

                    print(f"[Radar Poller] Found Ch.{latest} for {url} ({title})")
                    
                    if len(new_items) > 1:
                        latest_item = sorted(new_items, key=lambda x: x["num"])[-1]
                        latest_ch_num = float(latest_item["num"])
                        latest_ch_url = latest_item["url"]
                        latest_locked = latest_item["locked"]

                        if dl_en:
                            asyncio.create_task(self.trigger_auto_download(tid, latest_ch_num, latest_ch_url, gid, cid, title, mention_str, custom_msg))

                        ch = await self._get_or_fetch_channel(cid)
                        if ch:
                            cover_url = None
                            try:
                                cover_url = await asyncio.wait_for(self.provider_manager.get_series_cover(url), timeout=5)
                            except Exception:
                                pass

                            layout = build_tracker_batch_card_layout(
                                self.bot, url, new_items,
                                note="📥 جاري التحميل التلقائي للفصل الأحدث..." if dl_en else None,
                                cover_url=cover_url, series_title=title, view=self.tracker_card_view,
                                use_layout_api=False
                            )

                            role_ping = _resolve_subscription_mention(ch.guild, title, mention_str)
                            content_str = ""
                            if role_ping:
                                content_str = f"{role_ping} "
                            if custom_msg:
                                content_str += custom_msg

                            try:
                                sent = await ch.send(
                                    content=content_str.strip() if content_str.strip() else None,
                                    embed=layout.embed,
                                    view=layout,
                                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
                                )
                                import json
                                batch_data_str = json.dumps(new_items)
                                await database.upsert_tracker_card(
                                    message_id=sent.id, tracker_id=tid, guild_id=gid, channel_id=cid,
                                    url=url, chapter_num=float(latest_ch_num), chapter_url=str(latest_ch_url),
                                    locked=1 if latest_locked else 0, batch_data=batch_data_str
                                )
                            except Exception as send_ex:
                                print(f"[Radar check_one] Failed to send batch message for {title}: {send_ex}")
                                raise send_ex
                    else:
                        item = new_items[0]
                        ch_num = float(item["num"])
                        ch_url = str(item["url"])
                        locked = bool(item["locked"])

                        if dl_en:
                            asyncio.create_task(self.trigger_auto_download(tid, ch_num, ch_url, gid, cid, title, mention_str, custom_msg))

                        ch = await self._get_or_fetch_channel(cid)
                        if ch:
                            cover_url = None
                            try:
                                cover_url = await asyncio.wait_for(self.provider_manager.get_series_cover(url), timeout=5)
                            except Exception:
                                pass

                            layout = build_tracker_card_layout(
                                self.bot, url, float(ch_num), str(ch_url), locked,
                                note="📥 جاري التحميل التلقائي..." if dl_en else None,
                                cover_url=cover_url, series_title=title, view=self.tracker_card_view,
                                use_layout_api=False
                            )

                            role_ping = _resolve_subscription_mention(ch.guild, title, mention_str)
                            content_str = ""
                            if role_ping:
                                content_str = f"{role_ping} "
                            if custom_msg:
                                content_str += custom_msg

                            try:
                                sent = await ch.send(
                                    content=content_str or None,
                                    embed=layout.embed,
                                    view=layout,
                                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
                                )
                                await database.upsert_tracker_card(
                                    message_id=sent.id, tracker_id=tid, guild_id=gid, channel_id=cid,
                                    url=url, chapter_num=float(ch_num), chapter_url=str(ch_url), locked=1 if locked else 0
                                )
                            except Exception as send_ex:
                                print(f"[Radar check_one] Failed to send message for {title} Ch.{ch_num}: {send_ex}")
                                raise send_ex

                    new_heat = self.adaptive_poller.update_heat(heat_score, "new_chapter")
                    release_pattern = self.adaptive_poller.learn_schedule(release_pattern, now)

                    await database.update_tracker_chapter(tid, latest, now.isoformat())
                    await database.update_tracker_polling_state(tid, new_heat, priority, check_method, 0)
                    await database.update_tracker_release_pattern(tid, release_pattern, now.isoformat())

                except Exception as e:
                    print(f"[Radar Poller] ❌ Error checking {url}: {e}")
                    new_heat = self.adaptive_poller.update_heat(heat_score, "error")
                    failures = consecutive_failures + 1
                    await database.update_tracker_time(tid, now.isoformat())
                    await database.update_tracker_polling_state(tid, new_heat, priority, check_method, failures)

        await asyncio.gather(*[check_one(r) for r in due])

    @tasks.loop(seconds=30)
    async def global_feed_loop(self):
        await self.bot.wait_until_ready()
        try:
            trackers = await database.get_all_trackers()
            if not trackers:
                return

            mapped_trackers = []
            for r in trackers:
                if r[9]:  # paused
                    continue
                priority = r[13] if len(r) > 13 else "normal"
                heat_score = r[14] if len(r) > 14 else 50.0
                last_release_at = r[15] if len(r) > 15 else None
                release_pattern = r[16] if len(r) > 16 else None
                check_method = r[17] if len(r) > 17 else "scrape"
                consecutive_failures = r[18] if len(r) > 18 else 0

                mapped_trackers.append({
                    "tracker_id": r[0],
                    "guild_id": r[1],
                    "channel_id": r[2],
                    "url": r[3],
                    "last_chapter": r[4],
                    "custom_msg": r[5],
                    "interval_hours": r[6],
                    "last_checked": r[7],
                    "download_enabled": r[8],
                    "paused": r[9],
                    "title": r[10],
                    "mention_str": r[11],
                    "interval_minutes": r[12],
                    "priority": priority or "normal",
                    "heat_score": heat_score if heat_score is not None else 50.0,
                    "last_release_at": last_release_at,
                    "release_pattern": release_pattern,
                    "check_method": check_method or "scrape",
                    "consecutive_failures": consecutive_failures if consecutive_failures is not None else 0
                })

            matches = await self.feed_watcher.poll_global_feeds(mapped_trackers)
            if not matches:
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            for m in matches:
                await self.process_global_match(m, now)
        except Exception as e:
            print(f"[Radar Global Feed Loop] Error: {e}")

    async def process_global_match(self, match: dict, now: datetime.datetime):
        tid = match["tracker_id"]
        gid = match["guild_id"]
        cid = match["channel_id"]
        url = match["url"]
        new_ch = match["new_chapter"]
        ch_url = match["chapter_url"]
        locked = match["locked"]
        title = match["title"]
        mention_str = match["mention_str"]
        custom_msg = match["custom_msg"]
        dl_en = match["download_enabled"]

        # Deduplicate notifications
        key = (tid, float(new_ch))
        now_time = datetime.datetime.now(datetime.timezone.utc)
        self._recently_notified = {k: v for k, v in self._recently_notified.items() if (now_time - v).total_seconds() < 600}
        if key in self._recently_notified:
            print(f"[Radar Global Match] ⚠️ Alert already sent recently for {title} Ch.{new_ch}. Skipping duplicate.")
            return
        self._recently_notified[key] = now_time

        print(f"[Radar Global Match] Processing Ch.{new_ch} for {url} ({title})")
        
        if dl_en:
            asyncio.create_task(self.trigger_auto_download(tid, new_ch, ch_url, gid, cid, title, mention_str, custom_msg))

        ch = await self._get_or_fetch_channel(cid)
        if ch:
            cover_url = None
            try:
                cover_url = await asyncio.wait_for(self.provider_manager.get_series_cover(url), timeout=5)
            except Exception:
                pass

            layout = build_tracker_card_layout(
                self.bot, url, float(new_ch), str(ch_url), locked,
                note="📥 جاري التحميل التلقائي..." if dl_en else None,
                cover_url=cover_url, series_title=title, view=self.tracker_card_view,
                use_layout_api=False
            )

            role_ping = _resolve_subscription_mention(ch.guild, title, mention_str)
            content_str = ""
            if role_ping:
                content_str = f"{role_ping} "
            if custom_msg:
                content_str += custom_msg

            try:
                sent = await ch.send(
                    content=content_str or None,
                    embed=layout.embed,
                    view=layout,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
                )
                await database.upsert_tracker_card(
                    message_id=sent.id, tracker_id=tid, guild_id=gid, channel_id=cid,
                    url=url, chapter_num=float(new_ch), chapter_url=str(ch_url), locked=1 if locked else 0,
                )
            except Exception as send_ex:
                print(f"[Radar Global Match] Failed to send message for {title}: {send_ex}")
                raise send_ex

        new_heat = self.adaptive_poller.update_heat(match["heat_score"], "new_chapter")
        release_pattern = self.adaptive_poller.learn_schedule(match["release_pattern"], now)
        
        await database.update_tracker_chapter(tid, new_ch, now.isoformat())
        await database.update_tracker_polling_state(tid, new_heat, match["priority"], match["check_method"], 0)
        await database.update_tracker_release_pattern(tid, release_pattern, now.isoformat())

    async def trigger_auto_download(self, tracker_id: int, ch_num: float, ch_url: str, guild_id: int, channel_id: int, title: str, mention_str: str, custom_msg: str):
        key = (int(tracker_id), float(ch_num))
        if key in self._active_downloads:
            print(f"[Radar AutoDL] ⚠️ Download already in progress for tracker {tracker_id} Ch.{ch_num}. Skipping duplicate.")
            return
        self._active_downloads.add(key)
        
        try:
            async with self.autodl_semaphore:
                def_up = await database.get_setting("default_upload_dest", "Auto")
                remote_down = getattr(self.bot, "remote_down", None)
                downloader = getattr(self.bot, "downloader", None)
                dl_link = None
                
                # 1. Attempt remote worker download first if enabled
                if remote_down and getattr(remote_down, "is_enabled", False):
                    print(f"[Radar AutoDL] Attempting remote worker download for {title} Ch.{ch_num}...")
                    ch_name = f"Ch_{int(ch_num) if float(ch_num).is_integer() else ch_num}"
                    try:
                        job = await remote_down.start_download(
                            ch_url,
                            ch_name,
                            params={"upload_dest": def_up},
                        )
                        if "error" not in job:
                            result = await remote_down.wait_for_job(job["job_id"], max_wait_sec=600)
                            if result.get("status") == "completed":
                                dl_link = result.get("result")
                                print(f"[Radar AutoDL] ✅ Ch.{ch_num} completed via worker: {dl_link}")
                    except Exception as remote_err:
                        print(f"[Radar AutoDL] Remote download failed: {remote_err}. Trying local fallback...")
                
                # 2. Local Fallback download if remote failed or is disabled
                if not dl_link and downloader:
                    print(f"[Radar AutoDL] Attempting local fallback download for {title} Ch.{ch_num}...")
                    s_title_clean = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                    folder_name = f"{s_title_clean}_Ch_{_lbl(ch_num)}"
                    
                    try:
                        res = await downloader.download_and_stitch(
                            ch_url,
                            folder_name,
                            upload_dest=def_up
                        )
                        if res and res.get("link"):
                            dl_link = res["link"]
                            print(f"[Radar AutoDL] ✅ Ch.{ch_num} completed via local fallback: {dl_link}")
                    except Exception as local_err:
                        print(f"[Radar AutoDL] Local fallback download failed: {local_err}")

                if dl_link:
                    ch = await self._get_or_fetch_channel(channel_id)
                    if ch:
                        try:
                            # Try to find the original tracker card message and edit it
                            edited = False
                            try:
                                db = await database._get_db()
                                async with db.execute(
                                    "SELECT message_id, locked, url FROM tracker_cards WHERE tracker_id=? AND chapter_num=? LIMIT 1",
                                    (int(tracker_id), float(ch_num))
                                ) as cursor:
                                    row = await cursor.fetchone()
                                if row:
                                    msg_id, was_locked, series_url = row
                                    msg = await ch.fetch_message(msg_id)
                                    
                                    # Fetch cover if possible
                                    cover_url = None
                                    try:
                                        cover_url = await asyncio.wait_for(self.provider_manager.get_series_cover(series_url), timeout=3)
                                    except Exception:
                                        pass
                                        
                                    layout = build_tracker_card_layout(
                                        self.bot, series_url, float(ch_num), str(ch_url), bool(was_locked),
                                        note="✅ تم التحميل بنجاح ورُفع إلى Google Drive",
                                        cover_url=cover_url, series_title=title, view=self.tracker_card_view,
                                        use_layout_api=False, dl_link=dl_link
                                    )
                                    layout.embed.color = discord.Color.from_rgb(35, 165, 89)
                                    
                                    await msg.edit(embed=layout.embed, view=layout)
                                    edited = True
                                    print(f"[Radar AutoDL] Edited original card message {msg_id} with download link.")
                            except Exception as edit_err:
                                print(f"[Radar AutoDL] Failed to edit original card message: {edit_err}")
                                
                            # Fallback to sending a new temporary link notification if edit failed/message wasn't found
                            if not edited:
                                role_ping = _resolve_subscription_mention(ch.guild, title, mention_str)
                                content_str = ""
                                if role_ping:
                                    content_str = f"{role_ping} "
                                content_str += f"📥 **AutoDL Completed** for **{title}** Ch.`{_lbl(ch_num)}`:\n🔗 {dl_link}"
                                await ch.send(
                                    content=content_str,
                                    delete_after=3600,
                                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
                                )
                        except Exception:
                            pass
                else:
                    print(f"[Radar AutoDL] ❌ Both remote and local download failed for {title} Ch.{ch_num}")
                
        except Exception as dl_err:
            print(f"[Radar AutoDL] ❌ Error in trigger_auto_download: {dl_err}")
        finally:
            self._active_downloads.discard(key)

    @tasks.loop(seconds=30)
    async def reminder_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = await database.get_due_radar_reminders(now, limit=50)
        if not rows:
            return

        for (
            rid,
            message_id,
            tracker_id,
            guild_id,
            channel_id,
            user_id,
            notify_at,
        ) in rows:
            try:
                ch = await self._get_or_fetch_channel(int(channel_id))
                if not ch:
                    await database.mark_radar_reminder_fired(rid)
                    continue

                card = await database.get_tracker_card(int(message_id))
                series_url = card.get("url") if card else ""
                chapter_num = card.get("chapter_num") if card else None
                chapter_url = card.get("chapter_url") if card else None

                msg_link = (
                    f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
                )
                mention = f"<@{user_id}>"
                text = f"{mention} ⏰ Reminder\n"
                if chapter_num is not None and chapter_url:
                    text += f"Ch. `{_lbl(chapter_num)}`: {chapter_url}\n"
                text += f"🔗 {msg_link}"
                await ch.send(
                    content=text,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            except Exception as e:
                print(f"[Radar] reminder send failed: {e}")
            finally:
                await database.mark_radar_reminder_fired(rid)

    # ── أوامر ─────────────────────────────────────────────────────────────

    @app_commands.command(name="manga_panel", description="عرض لوحة تحكم السلسلة وتحديد الفصول للتحميل والتنزيل")
    @app_commands.describe(url="رابط المانجا أو السلسلة من أي موقع مدعوم")
    @vip_only()
    async def manga_panel_cmd(self, interaction: discord.Interaction, url: str):
        # إرسال إنبيد تحميل مبدئي بدلاً من Defer الافتراضي
        site_name = _domain(url).upper()

        load_em = discord.Embed(
            title="Fetching Series Info...",
            description=f"Loading **Series** from **{site_name}**...",
            color=C_IDLE,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(embed=load_em)
        else:
            # في حال كان ناتجاً عن ضغطة زر في واجهة أخرى
            await interaction.followup.send(embed=load_em, ephemeral=True)

        try:
            prov_name = self.provider_manager.get_provider_name(url)

            # جلب الفصول + صورة الغلاف بالتوازي
            chs_task = self.provider_manager.get_chapters_with_lock_info(url)
            cover_task = self.provider_manager.get_series_cover(url)
            chs_rich, cover_url = await asyncio.gather(
                chs_task, cover_task, return_exceptions=True
            )

            if isinstance(chs_rich, Exception):
                chs_rich = {}
            if isinstance(cover_url, Exception):
                cover_url = None

            # فصل الـ URL عن معلومات الإقفال
            chs = {}
            locked_set = set()
            for num, info in chs_rich.items():
                if isinstance(info, dict):
                    chs[num] = info["url"]
                    if info.get("locked"):
                        locked_set.add(num)
                else:
                    chs[num] = info

            if not chs:
                em_fail = discord.Embed(
                    title="❌ لم يُعثر على فصول",
                    description=(
                        f"```yaml\n"
                        f"  Site     : {_domain(url)}\n"
                        f"  Provider : {prov_name}\n"
                        f"  Error    : تعذّر جلب الفصول\n"
                        f"             تحقق من الرابط أو حاول لاحقاً\n"
                        f"```"
                    ),
                    color=C_FAIL,
                )
                return await interaction.edit_original_response(embed=em_fail)

            # Fetch default upload dest
            def_up = await database.get_setting("default_upload_dest", "Auto")

            view = MangaPanelView(
                self.bot,
                self.downloader,
                self.provider_manager,
                url,
                chs,
                requester=interaction.user,
                provider_name=prov_name,
                cover_url=cover_url,
                locked_chapters=locked_set,
                default_upload=def_up,
            )
            view._rebuild(
                f"✅ وُجد {len(chs)} فصل  ·  "
                f"Ch.{_lbl(min(chs))} → Ch.{_lbl(max(chs))}"
                + (f"  ·  🔒 {len(locked_set)} مدفوع" if locked_set else "")
            )
            await interaction.edit_original_response(embed=None, view=view)

        except Exception as e:
            try:
                await interaction.edit_original_response(
                    embed=discord.Embed(
                        title="❌ خطأ",
                        description=f"```\n{str(e)[:500]}\n```",
                        color=C_FAIL,
                    )
                )
            except Exception:
                pass
            import traceback

            traceback.print_exc()



    @tasks.loop(minutes=2)
    async def presence_updater_loop(self):
        await self.bot.wait_until_ready()
        try:
            # Check for active downloads in the downloader
            active_dls = list(getattr(self.downloader, "active_downloads", {}).values())
            if active_dls:
                # Show active download status
                active_job = active_dls[0]
                status_text = f"📥 Downloading {active_job}..."
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=status_text
                    )
                )
                return

            trackers_cnt = await database.get_tracker_count()
            db = await database._get_db()
            
            # Rotate status
            idx = self._status_index % 3
            self._status_index += 1

            if idx == 0:
                async with db.execute("SELECT COUNT(*) FROM stitch_jobs") as cursor:
                    row = await cursor.fetchone()
                    jobs_cnt = row[0] if row else 0
                status_text = f"📡 {trackers_cnt} trackers | 📥 {jobs_cnt} downloads"
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=status_text
                    )
                )
            elif idx == 1:
                # Last read/tracked series
                async with db.execute("SELECT title, last_chapter FROM trackers WHERE paused=0 AND title != '' ORDER BY last_checked DESC LIMIT 1") as cursor:
                    row = await cursor.fetchone()
                if row and row[0]:
                    title_manga, last_ch = row[0], row[1]
                    status_text = f"📖 Reading {title_manga} Ch. {_lbl(last_ch)}"
                    await self.bot.change_presence(
                        activity=discord.Activity(
                            type=discord.ActivityType.playing,
                            name=status_text
                        )
                    )
                else:
                    # Fallback to general stats
                    async with db.execute("SELECT COUNT(*) FROM stitch_jobs") as cursor:
                        row = await cursor.fetchone()
                        jobs_cnt = row[0] if row else 0
                    status_text = f"📡 {trackers_cnt} trackers | 📥 {jobs_cnt} downloads"
                    await self.bot.change_presence(
                        activity=discord.Activity(
                            type=discord.ActivityType.watching,
                            name=status_text
                        )
                    )
            else:
                # Completed stitch jobs
                async with db.execute("SELECT COUNT(*) FROM stitch_jobs WHERE status='completed'") as cursor:
                    row = await cursor.fetchone()
                    comp_cnt = row[0] if row else 0
                if comp_cnt > 0:
                    status_text = f"✅ {comp_cnt} chapters stitched"
                else:
                    status_text = f"📡 {trackers_cnt} trackers monitored"
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=status_text
                    )
                )
        except Exception as e:
            print(f"[Radar Status Loop] Error: {e}")

    @tasks.loop(minutes=15)
    async def locked_cards_updater_loop(self):
        await self.bot.wait_until_ready()
        try:
            locked_cards = await database.get_locked_tracker_cards(days_limit=7)
            if not locked_cards:
                return

            print(f"[Radar] Checking status of {len(locked_cards)} locked tracker cards...")
            urls_checked = {}
            for card in locked_cards:
                url = card["url"]
                message_id = card["message_id"]
                guild_id = card["guild_id"]
                channel_id = card["channel_id"]
                chapter_num = card["chapter_num"]
                chapter_url = card["chapter_url"]
                tracker_id = card["tracker_id"]

                if url not in urls_checked:
                    try:
                        rich = await self.provider_manager.get_chapters_with_lock_info(url)
                        urls_checked[url] = rich
                    except Exception as e:
                        print(f"[Radar] Error fetching chapters for {url} in locked loop: {e}")
                        urls_checked[url] = None

                rich = urls_checked[url]
                if not rich:
                    continue

                info = None
                for ch_n, ch_info in rich.items():
                    try:
                        if float(ch_n) == float(chapter_num):
                            info = ch_info
                            break
                    except (ValueError, TypeError):
                        continue

                if info and isinstance(info, dict):
                    currently_locked = bool(info.get("locked"))
                    if not currently_locked:
                        print(f"[Radar] Ch.{chapter_num} for {url} is now unlocked! Updating card...")
                        
                        await database.update_tracker_card_locked(message_id, locked=0)

                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if not channel:
                                try:
                                    channel = await self.bot.fetch_channel(int(channel_id))
                                except Exception:
                                    continue
                            
                            if not channel:
                                continue

                            try:
                                message = await channel.fetch_message(int(message_id))
                            except Exception:
                                continue

                            if not message:
                                continue
                            
                            tracker = await database.get_tracker(int(tracker_id), int(guild_id))
                            paused = bool(int(tracker[9] or 0)) if tracker and len(tracker) > 9 else False
                            series_title = tracker[10] if tracker and len(tracker) > 10 else None
                            
                            cover_url = None
                            try:
                                cover_url = await asyncio.wait_for(self.provider_manager.get_series_cover(url), timeout=5)
                            except Exception:
                                pass

                            layout = build_tracker_card_layout(
                                self.bot,
                                url,
                                float(chapter_num),
                                str(chapter_url),
                                locked=False,
                                note="🔓 تم فتح الفصل مجاناً الآن!",
                                paused=paused,
                                view=self.tracker_card_view,
                                cover_url=cover_url,
                                series_title=series_title,
                            )
                            
                            await message.edit(view=layout)
                            print(f"[Radar] Successfully updated Discord card for Ch.{chapter_num} ({url})")
                        except Exception as edit_err:
                            print(f"[Radar] Failed to update Discord card message {message_id}: {edit_err}")
        except Exception as loop_err:
            print(f"[Radar] Exception in locked_cards_updater_loop: {loop_err}")




async def setup(bot):
    await bot.add_cog(RadarCog(bot))
