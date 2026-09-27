"""رسائل وشعارات إكمال التحميل — بدون استيراد manga_downloader أو radar."""
import datetime
import discord
from discord import ui

C_DONE = discord.Color.from_rgb(35, 165, 89)
C_RUN = discord.Color.from_rgb(88, 101, 242)
C_FAIL = discord.Color.from_rgb(242, 63, 66)

LOGO_DRIVE = (
    "https://cdn.discordapp.com/emojis/1509000723042402365.webp?size=128"
)
LOGO_GOFILE = (
    "https://cdn.discordapp.com/emojis/1509882107176943777.webp?size=128"
)


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


def _provider_ui(provider: str, bot_avatar: str) -> tuple[str | None, str, str]:
    prov = provider or ""
    if "Drive" in prov:
        return "Drive", "Google Drive", LOGO_DRIVE
    if "Gofile" in prov:
        return "Gofile", "Gofile", LOGO_GOFILE
    return None, prov or "Download", bot_avatar


def build_progress_embed(
    *,
    title: str,
    phase: str,
    progress_bar: str,
    counter: str = "",
    provider: str = "—",
    size: str = "—",
    color: discord.Color | None = None,
    footer: str = "",
    bot_avatar: str = "",
) -> discord.Embed:
    """إنبيد تقدم للتحميل المباشر أو SmartStitch."""
    _, _, icon = _provider_ui(provider, bot_avatar)
    if "Drive" in (provider or ""):
        icon = LOGO_DRIVE
    elif "Gofile" in (provider or ""):
        icon = LOGO_GOFILE

    em = discord.Embed(
        title=title,
        color=color or C_RUN,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    em.set_author(name=f"⏳  {phase[:80]}", icon_url=icon or bot_avatar or None)
    lines = [
        f"```ansi\n [2;37mProgress [0m  [1;37m{progress_bar} [0m",
    ]
    if counter:
        lines[0] += f"  [2;37m{counter} [0m"
    lines[0] += "\n```"
    em.description = lines[0]
    em.add_field(name="Provider", value=f"`{provider}`", inline=True)
    em.add_field(name="Size", value=f"`{size}`", inline=True)
    if footer:
        em.set_footer(text=footer[:200])
    return em


def build_chapter_completion_embed(
    *,
    series_url: str,
    series_title: str,
    chapter_label: str,
    main_link: str,
    provider: str,
    cover_url: str | None,
    bot_avatar: str,
    multi_chapters: int = 0,
    chapter_range: str = "",
) -> tuple[discord.Embed, discord.ui.View]:
    dest_key, dest_label, logo = _provider_ui(provider, bot_avatar)

    em = discord.Embed(color=C_DONE, timestamp=datetime.datetime.now(datetime.timezone.utc))
    em.set_author(name="✅  Download completed", icon_url=bot_avatar)

    if cover_url and str(cover_url).strip().startswith("http"):
        em.set_image(url=str(cover_url).strip())

    em.title = series_title or _series_name(series_url)
    em.url = series_url

    if multi_chapters > 1 and chapter_range:
        em.description = f"**{multi_chapters} Chapters**  ·  `{chapter_range}`"
    else:
        em.description = f"**Chapter {chapter_label}**"

    if main_link and dest_key in ("Drive", "Gofile"):
        em.set_thumbnail(url=logo)
        folder_txt = (
            f"{dest_label} — Chapter Folder"
            if multi_chapters > 1
            else f"{dest_label} — Chapter"
        )
        em.add_field(name="\u200b", value=f"[**{folder_txt}**]({main_link})", inline=False)

    view = discord.ui.View()
    if main_link:
        if multi_chapters > 1 and dest_key == "Gofile":
            btn_lbl = "Open folder in Gofile"
        elif multi_chapters > 1 and dest_key == "Drive":
            btn_lbl = "Open folder in Google Drive"
        else:
            btn_lbl = "Open chapter folder" if multi_chapters > 1 else "Open chapter"
        view.add_item(ui.Button(label=btn_lbl, url=main_link, style=discord.ButtonStyle.link))
    return em, view


def build_stitch_completion_embed(
    *,
    title: str,
    main_link: str,
    provider: str,
    bot_avatar: str,
) -> tuple[discord.Embed, discord.ui.View]:
    em, view = build_chapter_completion_embed(
        series_url=main_link or "https://drive.google.com",
        series_title=title,
        chapter_label="Stitched",
        main_link=main_link,
        provider=provider,
        cover_url=None,
        bot_avatar=bot_avatar,
    )
    em.set_author(name="✅  SmartStitch completed", icon_url=bot_avatar)
    em.description = f"**SmartStitch**  ·  `{title}`"
    return em, view
