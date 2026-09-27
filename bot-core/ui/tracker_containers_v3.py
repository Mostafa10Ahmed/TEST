"""
tracker_containers_v3.py — ARCANE-style containers for the unified server tracker v3.

Two main containers:
  1. build_new_chapter_alert   → sent when a new chapter is detected (with role mention)
  2. build_download_complete   → sent when auto-download finishes (with personal mention)
"""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

import discord

# ── Colors ────────────────────────────────────────────────────────────────────
C_GOLD   = discord.Color.from_rgb(255, 184, 0)    # chapter available
C_GREEN  = discord.Color.from_rgb(35, 165, 89)    # download complete
C_RED    = discord.Color.from_rgb(242, 63, 66)    # locked / failed
C_PURPLE = discord.Color.from_rgb(124, 92, 252)   # paused
C_BLUE   = discord.Color.from_rgb(88, 101, 242)   # progress / info


# ── Helpers ───────────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _slug_to_name(url: str) -> str:
    """Solo-leveling-abc123 → Solo Leveling"""
    if "?" in url:
        url = url.split("?")[0]
    if "#" in url:
        url = url.split("#")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    skip = {"status", "detail", "chapters", "list", "webtoon", "manga", "series", "comic"}
    while parts and parts[-1].lower() in skip:
        parts.pop()
    if not parts:
        return "Series"
    slug = parts[-1]
    if re.search(r"chapter|ch-?\d|episode|ep-?\d", slug, re.I) and len(parts) > 1:
        slug = parts[-2]
    name = slug.replace("-", " ").replace("_", " ").title()
    name = re.sub(r"\s+[0-9a-f]{6,}\s*$", "", name, flags=re.I).strip()
    return name or "Series"


def _ch_label(num: float) -> str:
    try:
        return str(int(num)) if float(num).is_integer() else str(num)
    except Exception:
        return str(num)


def _time_ago(iso: str | None) -> str:
    if not iso:
        return "الآن"
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        diff = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        if diff < 60:
            return "منذ ثوانٍ"
        if diff < 3600:
            return f"منذ {int(diff // 60)} دقيقة"
        if diff < 86400:
            return f"منذ {int(diff // 3600)} ساعة"
        return f"منذ {int(diff // 86400)} يوم"
    except Exception:
        return "الآن"


def _add_cover(container: discord.ui.Container, cover_url: str | None) -> None:
    if cover_url and str(cover_url).startswith("http"):
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=cover_url))
        )


def _add_sep(container: discord.ui.Container) -> None:
    container.add_item(
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Container 1 — New Chapter Alert
# ═══════════════════════════════════════════════════════════════════════════════

def build_new_chapter_alert(
    *,
    series_title: str,
    series_url: str,
    chapter_num: float,
    chapter_url: str,
    locked: bool = False,
    has_cookies: bool = False,
    cover_url: str | None = None,
    detected_at: str | None = None,
) -> discord.ui.Container:
    """
    كونتينر إشعار فصل جديد — يُرسل مع منشن الرول.

    Layout:
      [Cover image]
      # 🔔 {series_title}
      فصل جديد صدر للتو! ...معلومات...
      ──────────
      [⬇️ تحميل] [📖 قراءة] [⏸️ إيقاف]
    """
    if locked and not has_cookies:
        color = C_RED
        status_icon = "🔒"
        status_txt = "مقفل — يتطلب اشتراكاً"
    elif locked and has_cookies:
        color = C_GREEN
        status_icon = "🔓"
        status_txt = "مقفل (كوكيز متوفرة — جاهز للتحميل)"
    else:
        color = C_GOLD
        status_icon = "🟢"
        status_txt = "متاح ومجاني"

    container = discord.ui.Container(accent_color=color)

    # Cover
    _add_cover(container, cover_url)

    # Title
    container.add_item(discord.ui.TextDisplay(f"# 🔔 {series_title}"))

    # Body
    time_str = _time_ago(detected_at)
    domain = _domain(series_url)
    body = (
        f"**✨ فصل جديد صدر للتو!**\n\n"
        f"📖 **الفصل الجديد**: [Ch. {_ch_label(chapter_num)}]({chapter_url})\n"
        f"🌐 **المصدر**: [{domain}]({series_url})\n"
        f"⚡ **الحالة**: {status_icon} **{status_txt}**\n\n"
        f"💡 **You can download this chapter** / **يمكنك تحميل هذا الفصل**"
    )
    container.add_item(discord.ui.TextDisplay(body))

    return container


def build_new_chapter_alert_buttons(
    *,
    tracker_id: int,
    chapter_num: float,
    chapter_url: str = "",
    locked: bool = False,
    has_cookies: bool = False,
) -> discord.ui.ActionRow:
    """
    صف الأزرار للكونتينر الأول.
    """
    buttons = []

    # Download Button (Success / Green)
    buttons.append(discord.ui.Button(
        label="Download",
        style=discord.ButtonStyle.success,
        custom_id=f"sv3_dl_{tracker_id}_{_ch_label(chapter_num)}",
    ))

    # Document Button (Secondary / Grey)
    buttons.append(discord.ui.Button(
        label="📄",
        style=discord.ButtonStyle.secondary,
        custom_id=f"sv3_doc_{tracker_id}_{_ch_label(chapter_num)}",
    ))

    return discord.ui.ActionRow(*buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# Container 2 — Download Completed
# ═══════════════════════════════════════════════════════════════════════════════

def build_download_complete(
    *,
    series_title: str,
    series_url: str,
    chapter_num: float,
    chapter_url: str,
    drive_url: str | None = None,
    cover_url: str | None = None,
    failed: bool = False,
    failed_reason: str = "",
) -> discord.ui.Container:
    """
    كونتينر اكتمال التحميل — يُرسل مع منشن المستخدم الشخصي.

    Layout (نجاح):
      # ✅ Download completed 🏆
      [Cover image]
      ### [Series Title](url)
      1 Chapter: Ch. 199
      ─────────
      ![Drive] Google Drive — Chapter Folder    [Open folder ↗]
      Want transcription? [Get Transcription]

    Layout (فشل):
      # ❌ فشل التحميل
      [Cover image]
      ### [Series Title](url)
      Ch. 199 — سبب الفشل
      ─────────
      [⬇️ إعادة المحاولة]
    """
    if failed:
        color = C_RED
        header = "# ❌ Download failed"
    else:
        color = C_GREEN
        header = "# Download completed 🟩"

    container = discord.ui.Container(accent_color=color)
    container.add_item(discord.ui.TextDisplay(header))

    # Cover
    _add_cover(container, cover_url)

    # Series name
    if series_url and series_url.startswith("http"):
        container.add_item(discord.ui.TextDisplay(f"### [{series_title}]({series_url})"))
    else:
        container.add_item(discord.ui.TextDisplay(f"### {series_title}"))

    # Chapter info
    ch_line = f"**1 Chapter: Ch. {_ch_label(chapter_num)}**"
    if failed and failed_reason:
        ch_line += f"\n-# {failed_reason}"
    container.add_item(discord.ui.TextDisplay(ch_line))

    if not failed and drive_url:
        _add_sep(container)
        # Drive folder section (ARCANE-style: logo + text left, button right)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "![Drive](https://ssl.gstatic.com/images/branding/product/1x/drive_2020q4_32dp.png)"
                    f" [**Google Drive — Server Folder**]({drive_url})"
                ),
                accessory=discord.ui.Button(
                    label="Open folder in Google Drive",
                    url=drive_url,
                    style=discord.ButtonStyle.link,
                ),
            )
        )

    return container


def build_download_complete_buttons(
    *,
    tracker_id: int,
    chapter_num: float,
    chapter_url: str = "",
    failed: bool = False,
) -> discord.ui.ActionRow:
    """أزرار كونتينر الإتمام."""
    buttons = []
    if failed:
        buttons.append(discord.ui.Button(
            label="Download",
            style=discord.ButtonStyle.success,
            custom_id=f"sv3_dl_{tracker_id}_{_ch_label(chapter_num)}",
        ))
        buttons.append(discord.ui.Button(
            label="📄",
            style=discord.ButtonStyle.secondary,
            custom_id=f"sv3_doc_{tracker_id}_{_ch_label(chapter_num)}",
        ))
    else:
        buttons.append(discord.ui.Button(
            label="📄",
            style=discord.ButtonStyle.secondary,
            custom_id=f"sv3_doc_{tracker_id}_{_ch_label(chapter_num)}",
        ))

    return discord.ui.ActionRow(*buttons)


# ═══════════════════════════════════════════════════════════════════════════════
# Container 3 — Admin Panel (قائمة التراكرز)
# ═══════════════════════════════════════════════════════════════════════════════

def build_panel_list(
    trackers: list[dict],
    *,
    page: int = 0,
    per_page: int = 10,
    guild_name: str = "",
) -> discord.ui.Container:
    """ملخص لوحة الأدمن مع إحصائيات سريعة."""
    total = len(trackers)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_items = trackers[page * per_page: (page + 1) * per_page]
    active_count = sum(1 for t in trackers if not t.get("paused"))
    paused_count = total - active_count
    auto_count = sum(1 for t in trackers if t.get("auto_download"))
    locked_count = sum(1 for t in trackers if t.get("check_method") and t.get("check_method") != "auto")

    container = discord.ui.Container(accent_color=C_BLUE)
    container.add_item(discord.ui.TextDisplay(
        f"# 📡 لوحة التتبع — {guild_name}\n"
        f"-# {total} متابعة · {active_count} نشطة · {paused_count} موقوفة · {auto_count} تحميل تلقائي · {locked_count} تعتمد فحصاً غير تلقائي\n"
        f"-# الصفحة الحالية {page + 1}/{total_pages}"
    ))

    if not page_items:
        container.add_item(discord.ui.TextDisplay(
            "لا توجد متابعات في هذه الصفحة.\n"
            "استخدم زر `➕ إضافة تتبع` لإنشاء أول متابعة."
        ))
        return container

    lines = []
    for i, t in enumerate(page_items, page * per_page + 1):
        paused = bool(t.get("paused"))
        auto_dl = bool(t.get("auto_download"))
        icon = "⏸️" if paused else "🟢"
        title = (t.get("title") or _slug_to_name(t["url"]))[:38]
        ch = _ch_label(t.get("last_chapter", 0))
        domain = _domain(t["url"])
        notif = f"<#{t['notification_channel_id']}>" if t.get("notification_channel_id") else "—"
        mode = "تلقائي" if auto_dl else "يدوي"

        lines.append(
            f"{icon} **{i:02d}.** [{title}]({t['url']})\n"
            f"└─ `Ch. {ch}` · `{domain}` · `{mode}` · روم: {notif}"
        )

    if page_items:
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay("\n\n".join(lines[:3])))
        if len(page_items) > 3:
            container.add_item(discord.ui.TextDisplay(f"-# +{len(page_items) - 3} سلاسل أخرى في هذه الصفحة"))
    return container


def build_panel_empty() -> discord.ui.Container:
    """لوحة فارغة — لا توجد متابعات."""
    c = discord.ui.Container(accent_color=C_BLUE)
    c.add_item(discord.ui.TextDisplay(
        "# 📡 لوحة التتبع\n\n"
        "لا توجد متابعات في هذا السيرفر بعد.\n"
        "ابدأ من زر `➕ إضافة تتبع` داخل لوحة `/tracker`."
    ))
    return c


def build_progress_bar(current_ch: float, target_chapters: float = 100.0) -> str:
    try:
        cur = float(current_ch or 0.0)
        target = float(target_chapters) if target_chapters > 0 else 100.0
        pct = min(100, max(0, int((cur / target) * 100)))
        filled = pct // 10
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        return f"`{bar}` **{pct}%**"
    except Exception:
        return "`░░░░░░░░░░` **0%**"


def build_tracker_detail(tracker: dict, sub_count: int = 0) -> discord.ui.Container:
    """كونتينر فاخر لتفاصيل تراكر واحد مع شريط التقدم وصورة الغلاف والمشتركين."""
    paused = bool(tracker.get("paused"))
    auto_dl = bool(tracker.get("auto_download"))
    title = tracker.get("title") or _slug_to_name(tracker.get("url", ""))
    color = C_PURPLE if paused else C_GOLD

    c = discord.ui.Container(accent_color=color)
    raw_cover = tracker.get("cover_url")
    if not raw_cover or not str(raw_cover).startswith("http"):
        raw_cover = "https://cdn.discordapp.com/embed/avatars/0.png"
    _add_cover(c, raw_cover)

    ch_val = float(tracker.get("last_chapter", 0) or 0)
    prog_bar = build_progress_bar(ch_val)

    c.add_item(discord.ui.TextDisplay(f"# {'⏸️' if paused else '🟢'} {title}"))

    role_str = f"<@&{tracker['mention_role_id']}>" if tracker.get("mention_role_id") else "لا يوجد"
    ch_str = f"#{tracker.get('id', '—')}"
    notif_chan = f"<#{tracker['notification_channel_id']}>" if tracker.get("notification_channel_id") else "—"
    lines = [
        f"🌐 **المصدر**: [{_domain(tracker.get('url', ''))}]({tracker.get('url', '')})",
        f"📖 **الفصل الحالي**: `Ch. {_ch_label(ch_val)}`",
        f"📊 **نسبة التقدم**: {prog_bar}",
        f"🔔 **المشتركون بالخدمة**: `{sub_count}` عضو 👤",
        f"📢 **روم الإشعارات**: {notif_chan}",
        f"👥 **رول المنشن**: {role_str}",
        f"📥 **التحميل التلقائي**: `{'✅ نشط' if auto_dl else '❌ معطل'}`",
    ]
    c.add_item(discord.ui.TextDisplay("\n".join(lines)))
    return c


def build_user_profile_container(
    *,
    user_name: str,
    rank_label: str,
    clean_daily: int,
    extract_daily: int,
    clean_credits: int,
    extract_credits: int,
    vip_expiry_str: str | None,
    reset_hours: int = 4,
) -> discord.ui.Container:
    """Builds a luxury User Profile & Credits balance container card."""
    c = discord.ui.Container(accent_color=C_BLUE)
    c.add_item(discord.ui.TextDisplay(f"# 👤 ملف ورصيد العضو: {user_name}"))
    c.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
    
    c_used = min(5, max(0, clean_daily))
    e_used = min(5, max(0, extract_daily))
    clean_bar = "█" * int(c_used * 2) + "░" * (10 - int(c_used * 2))
    extract_bar = "█" * int(e_used * 2) + "░" * (10 - int(e_used * 2))
    
    exp_info = f"`{vip_expiry_str}`" if vip_expiry_str else "`دائم / غير محدد`"
    
    body = (
        f"• **الرتبة الحالية**: **{rank_label}**\n"
        f"• **صلاحية الـ VIP**: {exp_info}\n\n"
        f"📊 **الاستهلاك اليومي الحالي (تتجدد تلقائياً):**\n"
        f"  🧹 **التبييض اليومي**: `{clean_bar}` `{c_used}/5` فصول\n"
        f"  📝 **الاستخراج اليومي**: `{extract_bar}` `{e_used}/5` فصول\n\n"
        f"💎 **رصيد النقاط الإضافية المشحونة (ثابتة لا تنتهي):**\n"
        f"  🎨 **نقاط التبييض المشحونة**: `{clean_credits}` نقطة\n"
        f"  📖 **نقاط الاستخراج المشحونة**: `{extract_credits}` نقطة\n\n"
        f"⏰ **التجديد اليومي القادم**: خلال تقريباً `{reset_hours}` ساعات"
    )
    c.add_item(discord.ui.TextDisplay(body))
    return c


