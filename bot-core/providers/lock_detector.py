"""
lock_detector.py — كاشف الفصول المجانية والمدفوعة

يعمل على HTML soup ويرجع:
    {"locked": True/False, "reason": "نص السبب"}

يدعم:
 • CSS selectors لأيقونات القفل في 50+ موقع
 • نصوص دالة: UP, Early Access, Fast Pass, Coin, Premium, Unlock
 • HTML attributes: data-locked, data-free, data-access
 • قواعد خاصة بكل موقع (Tapas, Webtoons, Lezhin, MangaPlus...)
"""

from __future__ import annotations
from bs4 import BeautifulSoup, Tag
import re


# ── Selectors لأيقونات القفل ───────────────────────────────────────────
LOCK_SELECTORS = [
    # عامة — lock icons
    ".fa-lock", ".icon-lock", "i.lock", ".lock-icon", ".locked-icon",
    "[class*='lock']", "[class*='Lock']",
    "svg[class*='lock']", "svg[data-icon='lock']",
    # attributes
    "[data-locked='true']", "[data-free='false']", "[data-accessible='false']",
    "[data-access='locked']",
    # نصوص بادج
    ".badge-lock", ".badge-locked", ".badge-paid", ".badge-premium",
    ".premium-tag", ".pay-tag", ".coin-tag", ".paid-mark",
    # Tapas
    ".js-payment-wall", ".content-paywall", ".ep-epub-lock", ".content-lock",
    "[class*='paywall']", "[class*='pay-wall']",
    # Webtoons
    ".fas.fa-lock", ".ico_lock", ".lk_lock", ".price-tag",
    "[class*='fastpass']", "[class*='fast-pass']",
    # Lezhin
    ".lz-icon-lock", ".point-required", "[data-coin-required]",
    # MangaPlus
    ".manga-expired", "[class*='blocked']",
    # Naver / Korean
    ".ic_lock", ".thumb_info .lock",
    # Toomics / Toptoon
    ".toon_lock", ".ico-lock", ".layer_lock",
    # KakaoPage
    ".ico_lock", ".item_wrap .lock",
    # Piccoma
    ".icon-lock-mini", ".js-lock",
    # LINE Manga
    ".coin-required", ".lock-overlay",
    # iQiyi
    ".vip-mark", ".iqy-lock",
]

# ── نصوص دالة على الإقفال ─────────────────────────────────────────────
LOCK_TEXTS = [
    # english
    r"\bearly\s+access\b",
    r"\bfast\s+pass\b",
    r"\bfast-pass\b",
    r"\bpremium\b",
    r"\bcoins?\s+required\b",
    r"\bunlock\s+with\b",
    r"\bpurchase\s+to\s+read\b",
    r"\bbuy\s+chapter\b",
    r"\bpaid\s+chapter\b",
    r"\blocked\s+chapter\b",
    r"\brequires?\s+subscription\b",
    r"\bsubscribe\s+to\s+read\b",
    r"\bvip\s+(only|chapter|content)\b",
    # korean
    r"코인\s*필요",   # coins required
    r"잠금",          # locked
    r"유료",          # paid
    # japanese
    r"コイン",        # coins
    r"ロック",        # lock
    # chinese
    r"需要.*?金币",   # need coins
    r"付费",          # paid
    # arabic
    r"مدفوع",
    r"مقفل",
    r"اشتراك",
]
_LOCK_TEXT_RE = re.compile("|".join(LOCK_TEXTS), re.I | re.UNICODE)

# ── Selectors الدالة على الحرية ────────────────────────────────────────
FREE_SELECTORS = [
    ".free-badge", ".badge-free", ".ico_free", ".ic_free",
    "[data-free='true']", "[data-locked='false']",
    ".free-episode", ".free-chapter",
]

FREE_TEXTS = [
    r"\bfree\b",
    r"\bfree\s+chapter\b",
    r"\bfree\s+episode\b",
    r"무료",      # korean: free
    r"無料",      # japanese: free
    r"免费",      # chinese: free
    r"مجاني",     # arabic
]
_FREE_TEXT_RE = re.compile("|".join(FREE_TEXTS), re.I | re.UNICODE)


def detect_lock_from_element(el: Tag) -> dict:
    """
    يكشف حالة القفل من عنصر واحد (li, div, a) يمثّل فصلاً.
    يرجع {"locked": bool, "reason": str}
    """
    html_str = str(el)
    text     = el.get_text(" ", strip=True)

    # 1. فحص CSS selectors للقفل
    for sel in LOCK_SELECTORS:
        if el.select_one(sel):
            return {"locked": True, "reason": f"selector:{sel}"}

    # 2. فحص attributes مباشرة
    attrs = el.attrs
    if attrs.get("data-locked") in ("true", "1", True):
        return {"locked": True, "reason": "data-locked=true"}
    if attrs.get("data-free") in ("false", "0", False):
        return {"locked": True, "reason": "data-free=false"}

    # 3. class names
    classes = " ".join(attrs.get("class", []))
    if re.search(r'\blocked\b|\bpaywall\b|\bpremium\b', classes, re.I):
        return {"locked": True, "reason": f"class:{classes}"}

    # 4. فحص CSS selectors للحرية
    for sel in FREE_SELECTORS:
        if el.select_one(sel):
            return {"locked": False, "reason": f"free-selector:{sel}"}

    # 5. نصوص الحرية (أولوية لأنها أكثر تحديداً)
    if _FREE_TEXT_RE.search(text):
        return {"locked": False, "reason": "free-text"}

    # 6. نصوص القفل
    if _LOCK_TEXT_RE.search(text):
        return {"locked": True, "reason": "lock-text"}

    return {"locked": False, "reason": "default-free"}


def detect_lock_from_html(html: str, chapter_url: str = "") -> dict:
    """
    يكشف حالة القفل من HTML صفحة الفصل كاملة.
    يُستخدم عندما لا يكون عندنا عنصر HTML للفصل.
    """
    soup = BeautifulSoup(html, "html.parser")

    # تحقق من paywall / lock overlays في الصفحة
    for sel in LOCK_SELECTORS + [
        ".paywall", ".content-paywall", ".locked-content",
        "#paywall", "#locked", ".payment-required",
    ]:
        if soup.select_one(sel):
            return {"locked": True, "reason": f"page:{sel}"}

    body_text = soup.get_text(" ", strip=True)[:2000]
    if _LOCK_TEXT_RE.search(body_text):
        return {"locked": True, "reason": "page-text"}

    return {"locked": False, "reason": "page-default-free"}


def bulk_detect(
    soup: BeautifulSoup,
    chapter_selectors: list[str] | None = None,
) -> dict[str, dict]:
    """
    يمسح صفحة القائمة كاملة ويرجع قاموس:
        { chapter_url: {"locked": bool, "reason": str} }

    chapter_selectors: قائمة بـ CSS selectors لعناصر الفصول.
    إذا لم تُعطَ، يجرب selectors شائعة.
    """
    default_selectors = [
        # Madara WordPress
        "li.wp-manga-chapter",
        ".chapters-list li",
        ".chapter-list li",
        ".chapter-list-item",
        # Tapas / Webtoon
        ".episode-list li",
        ".episode_list li",
        "[class*='episode-item']",
        "[class*='ep-item']",
        # Generic
        ".chapter-row",
        ".chp-row",
        "tr.chapter",
        "li[class*='chapter']",
        # MangaDex style
        ".chapter-container",
        "[data-chapter]",
    ]
    selectors = chapter_selectors or default_selectors

    results: dict[str, dict] = {}
    for sel in selectors:
        items = soup.select(sel)
        if not items:
            continue
        for item in items:
            # جلب رابط الفصل
            a = item.find("a", href=True)
            if not a:
                continue
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            info = detect_lock_from_element(item)
            results[href] = info
        if results:
            break   # أول selector يعمل يكفي

    return results


# ── قواعد خاصة بالمواقع ───────────────────────────────────────────────
SITE_RULES: dict[str, dict] = {
    "asura": {
        "lock_selector"  : "div.flex-shrink-0.w-8.h-8 svg.text-amber-400",
        "free_selector"  : "div.flex-shrink-0.w-8.h-8 svg.text-emerald-500",
        "episode_sel"    : "a.group.flex.items-center",
        "link_sel"       : "self",
        "num_from_url"   : r"/chapter/(\d+)",
    },
    "tapas.io": {
        "lock_selector"  : ".lock-icon, .js-payment-wall, .content-locked",
        "free_selector"  : ".ico-free, .free-badge",
        "episode_sel"    : ".episode-list .item",
        "link_sel"       : "a.link",
        "num_attr"       : "data-episode-id",
        "num_from_url"   : r"/episode/(\d+)",
    },
    "webtoons.com": {
        "lock_selector"  : ".lk_lock, .ico_lock, [class*='fastpass']",
        "free_selector"  : ".ico_free, .lk_free",
        "episode_sel"    : "#_listUl li",
        "link_sel"       : "a",
        "num_from_url"   : r"episode_no=(\d+)",
        "paginate_param" : "page",
    },
    "lezhin.com": {
        "lock_selector"  : ".lz-icon-lock, [data-coin-required]",
        "free_selector"  : ".free",
        "episode_sel"    : ".js-episode",
        "link_sel"       : "a",
        "num_attr"       : "data-episode",
    },
    "naver.com": {
        "lock_selector"  : ".ic_lock, .thumb_area .ic_lock",
        "free_selector"  : ".ic_free",
        "episode_sel"    : "#content .item",
        "link_sel"       : "a",
        "num_from_url"   : r"no=(\d+)",
        "paginate_param" : "page",
    },
    "toomics.com": {
        "lock_selector"  : ".toon_lock, .lock-layer",
        "free_selector"  : ".free",
        "episode_sel"    : ".list-chapter li",
        "link_sel"       : "a",
        "num_from_url"   : r"/(\d+)$",
    },
    "piccoma.com": {
        "lock_selector"  : ".icon-lock-mini, .js-lock",
        "free_selector"  : ".icon-free",
        "episode_sel"    : ".item-episode",
        "link_sel"       : "a",
        "num_from_url"   : r"/episode/(\d+)",
    },
}


def get_site_rule(url: str) -> dict:
    """إرجاع قاعدة الموقع بناءً على الـ URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        for key, rule in SITE_RULES.items():
            if key in host:
                return rule
    except Exception:
        pass
    return {}
