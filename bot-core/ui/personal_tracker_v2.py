"""Personal tracker UI components — containers & layout for /mytrack panel."""
from __future__ import annotations

import datetime
import math
from urllib.parse import urlparse

import discord

from download_ui import LOGO_DRIVE, LOGO_GOFILE
from services.intervals import format_check_interval

C_GOLD = discord.Color.from_rgb(255, 193, 7)
C_GREY = discord.Color.from_rgb(148, 156, 164)
C_RED = discord.Color.from_rgb(242, 63, 66)
C_GREEN = discord.Color.from_rgb(35, 165, 89)
C_PURPLE = discord.Color.from_rgb(114, 137, 218)
C_BLUE = discord.Color.from_rgb(88, 101, 242)
C_PANEL = discord.Color.from_rgb(255, 193, 7)


def display_name(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def chapter_label(v: float) -> str:
    return f"{v:g}" if v == int(v) else f"{v}"


def format_unlock_time(unlock_time: float | None) -> str:
    """تحول Unix timestamp إلى نص مثل 'يفتح بعد 3 ساعات'"""
    if not unlock_time:
        return ""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    remaining = unlock_time - now
    if remaining <= 0:
        return "🟢 متاح الآن"
    if remaining < 60:
        return "يفتح بعد دقيقة"
    if remaining < 3600:
        return f"يفتح بعد {int(remaining // 60)} دقيقة"
    if remaining < 7200:
        return f"يفتح بعد ساعة و {int((remaining % 3600) // 60)} دقيقة"
    if remaining < 86400:
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        return f"يفتح بعد {h} ساعة و {m} دقيقة" if m else f"يفتح بعد {h} ساعات"
    days = int(remaining // 86400)
    return f"يفتح بعد {days} يوم"


def build_series_container(
    *,
    tracker_id: int,
    user_id: int,
    url: str,
    title: str,
    last_chapter: float,
    latest_chapter: float | None = None,
    locked_count: int = 0,
    unlock_time: float | None = None,
    interval_minutes: int = 60,
    auto_download: bool = False,
    paused: bool = False,
    notification_channel: str = "",
    mention_on_update: bool = True,
    locked: bool = False,
    has_new: bool = False,
    cover_url: str | None = None,
    extra_note: str = "",
    predicted_release: str = "",
) -> discord.ui.Container:
    color = C_PURPLE if paused else (C_RED if locked else (C_GOLD if has_new else C_GREY))
    container = discord.ui.Container(accent_color=color)

    status_icon = "⏸️" if paused else ("🔒" if locked else ("🟢" if has_new else "⚪"))
    container.add_item(discord.ui.TextDisplay(f"# {status_icon} {title}"))

    domain = display_name(url)

    lines = []
    lines.append(f"🌐 **الموقع**: [{domain}]({url})")
    lines.append("")

    # 1. Reading Info
    lines.append("📖 **معلومات القراءة والفصول:**")
    lc = chapter_label(last_chapter)
    if latest_chapter is not None and latest_chapter > last_chapter:
        new_count = int(latest_chapter - last_chapter)
        lines.append(f"└─ آخر فصل قرأته: `{lc}` ← 🆕 **{chapter_label(latest_chapter)}** (+{new_count} فصول جديدة)")
    else:
        lines.append(f"└─ آخر فصل قرأته: `{lc}` (مواكب بالكامل)")
    
    if locked:
        unlock_str = format_unlock_time(unlock_time)
        lines.append(f"└─ حالة الموقع: 🔒 مقفل — {unlock_str}" if unlock_str else "└─ حالة الموقع: 🔒 مقفل")
    else:
        lines.append("└─ حالة الموقع: 🟢 متاح ومجاني")
    lines.append("")

    # 2. Polling & AutoDL
    lines.append("⏱️ **الفحص والتحميل التلقائي:**")
    lines.append(f"├─ وتيرة الفحص: كل `{format_check_interval(interval_minutes)}`")
    if predicted_release:
        lines.append(f"├─ التوقع القادم: `{predicted_release}`")
    lines.append(f"└─ التحميل التلقائي: {'✅ نشط وعامل' if auto_download else '❌ معطل'}")
    lines.append("")

    # 3. Notifications
    lines.append("🔔 **الإشعارات والتنبيهات:**")
    ch_mention = notification_channel if notification_channel else "لا توجد قناة مخصصة"
    lines.append(f"├─ روم الإشعارات: {ch_mention}")
    lines.append(f"└─ منشن عند التحديث: {'✅ مفعل' if mention_on_update else '❌ معطل'}")

    if extra_note:
        lines.append("")
        lines.append(f"📝 **ملاحظة إضافية:**\n`{extra_note}`")

    container.add_item(discord.ui.TextDisplay("\n".join(lines)))

    if cover_url and cover_url.startswith("http"):
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=cover_url))
        )

    return container


def build_list_container(
    page_trackers: list[dict],
    page: int,
    total_pages: int,
    total_count: int,
    lock_cache: dict[int, dict],
) -> discord.ui.Container:
    container = discord.ui.Container(accent_color=C_PANEL)
    container.add_item(discord.ui.TextDisplay("# 📡 لوحة التتبع الشخصية"))
    
    if not page_trackers:
        container.add_item(discord.ui.TextDisplay("لا توجد أي متابعات مضافة بعد.\nاضغط **➕ إضافة** للبدء."))
        return container

    lines = []
    lines.append(f"📊 إجمالي المتابعات: `{total_count}` · صفحة `{page + 1}/{total_pages}`\n")
    
    start_idx = page * 25 + 1
    for idx, t in enumerate(page_trackers, start_idx):
        tid = t["id"]
        lc = lock_cache.get(tid, {})
        locked = lc.get("locked", False)
        paused = bool(t["paused"])
        
        status_icon = "⏸️" if paused else ("🔒" if locked else "🟢")
        title = (t["title"] or "بدون عنوان")[:40]
        last_ch = chapter_label(t["last_chapter"])
        domain = display_name(t["url"])
        
        lines.append(f"{status_icon} **{idx:02d}.** `{title}`")
        lines.append(f"└─ الفصل: `{last_ch}` · الموقع: `{domain}` · تلقائي: `{'✅' if t['auto_download'] else '❌'}`")
        
    container.add_item(discord.ui.TextDisplay("\n".join(lines)))
    return container


def build_add_section_container(on_add_custom_id: str, on_refresh_all_custom_id: str) -> discord.ui.Container:
    container = discord.ui.Container(accent_color=C_PANEL)
    row = discord.ui.ActionRow(
        discord.ui.Button(label="➕ إضافة متابعة", style=discord.ButtonStyle.success, custom_id=on_add_custom_id),
        discord.ui.Button(label="🔄 فحص الكل", style=discord.ButtonStyle.primary, custom_id=on_refresh_all_custom_id),
    )
    container.add_item(row)
    return container


def build_empty_panel() -> discord.ui.Container:
    container = discord.ui.Container(accent_color=C_GREY)
    container.add_item(discord.ui.TextDisplay("# 📡 لوحة التتبع الشخصية"))
    container.add_item(discord.ui.TextDisplay("لا توجد متابعات بعد.\nاضغط **➕ إضافة متابعة** للبدء."))
    return container


def build_notification_container(
    *,
    series_name: str,
    series_url: str,
    chapter_num: float,
    chapter_url: str,
    main_link: str | None = None,
    cover_url: str | None = None,
    failed: bool = False,
    failed_reason: str = "",
) -> discord.ui.Container:
    container = discord.ui.Container(accent_color=C_RED if failed else C_GREEN)

    if failed:
        container.add_item(discord.ui.TextDisplay("# ❌ فشل التحميل التلقائي"))
        reason_str = failed_reason or "فشل التحميل من المصدر أو تعذر معالجته"
        container.add_item(discord.ui.TextDisplay(f"⚠️ **السبب**: `{reason_str}`"))
    else:
        container.add_item(discord.ui.TextDisplay("# ✅ تم التحميل والرفع بنجاح"))

    if cover_url and cover_url.startswith("http"):
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=cover_url))
        )

    desc_text = (
        f"📖 **الفصل**: [Ch. {chapter_label(chapter_num)}]({chapter_url})\n"
        f"📌 **السلسلة**: [{series_name}]({series_url})\n"
    )
    if not failed and main_link:
        dest = "Google Drive" if "drive" in main_link.lower() else "Gofile"
        desc_text += f"📥 **مكان الرفع**: `{dest}`\n"

    container.add_item(discord.ui.TextDisplay(desc_text))

    if main_link and not failed:
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        dest = "Google Drive" if "drive" in main_link.lower() else "Gofile"
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(f"📥 **رابط المشاهدة والتحميل:**\n└─ تم رفعه إلى {dest}"),
                accessory=discord.ui.Button(label="📖 فتح التحميل", url=main_link, style=discord.ButtonStyle.link),
            )
        )

    return container


def build_new_chapter_container(
    *,
    series_name: str,
    series_url: str,
    chapter_num: float,
    chapter_url: str,
    locked: bool = False,
    has_cookies: bool = False,
    cover_url: str | None = None,
) -> discord.ui.Container:
    if locked:
        color = C_GREEN if has_cookies else C_RED
        status_icon = "🔒"
        status_txt = "مقفل (كوكيز متوفرة — جاهز للتحميل)" if has_cookies else "مقفل (يتطلب كوكيز — استخدم /auth_panel)"
    else:
        color = C_GOLD
        status_icon = "🟢"
        status_txt = "متاح ومجاني"

    container = discord.ui.Container(accent_color=color)
    container.add_item(discord.ui.TextDisplay(f"# 🔔 تحديث جديد: {series_name}"))
    
    if cover_url and cover_url.startswith("http"):
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=cover_url))
        )
        
    desc_text = (
        f"✨ **فصل جديد متوفر الآن!**\n\n"
        f"📖 **الفصل الجديد**: [Ch. {chapter_label(chapter_num)}]({chapter_url})\n"
        f"📌 **السلسلة**: [{series_name}]({series_url})\n"
        f"🛡️ **حالة الفصل**: {status_icon} **{status_txt}**\n\n"
        f"⚡ *استخدم الأزرار أدناه للتحميل المباشر أو الفحص السريع أو تعديل الإعدادات.*"
    )
    container.add_item(discord.ui.TextDisplay(desc_text))
    return container

