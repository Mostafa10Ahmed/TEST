from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class CustomSelectorRule:
    domain: str
    selector: str               # "css:..." أو "xpath:..." أو بدون prefix (يُعامل css)
    url_attr: str = "href"      # attribute لاستخراج رابط الفصل
    number_regex: str = ""      # regex لاستخراج رقم الفصل (يفضل مجموعة واحدة)
    get_first: bool = False     # إذا كان هناك أكثر من نتيجة: أول/آخر
    use_browser: bool = False   # استخدم Playwright بدل HTTP البسيط
    notes: str = ""
    raw_config: str = ""


def _abs_url(base_url: str, href: str) -> str:
    try:
        from urllib.parse import urljoin
        return urljoin(base_url, href)
    except Exception:
        return href


def _extract_number(raw: str, number_regex: str) -> Optional[float]:
    txt = (raw or "").strip()
    if not txt:
        return None
    if number_regex:
        try:
            m = re.search(number_regex, txt, re.I)
            if not m:
                return None
            val = m.group(1) if m.groups() else m.group(0)
            return float(val)
        except Exception:
            return None
    # fallback: حاول التقاط رقم عشري من النص
    m2 = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m2:
        return None
    try:
        return float(m2.group(1))
    except Exception:
        return None


def parse_latest_from_html(html: str, base_url: str, rule: CustomSelectorRule) -> tuple[Optional[float], str, str]:
    """
    يرجع: (chapter_num, chapter_url, reason)
    """
    selector = (rule.selector or "").strip()
    if not selector and not rule.raw_config:
        return None, "", "no-selector"

    mode = "css"
    expr = selector
    if selector.startswith("css:"):
        mode, expr = "css", selector[4:].strip()
    elif selector.startswith("xpath:"):
        mode, expr = "xpath", selector[6:].strip()

    config_json = (rule.raw_config or "").strip()
    if not config_json and selector.startswith("{"):
        config_json = selector

    # Support parsing Kotatsu-style or dynamic raw selector configurations if it is JSON
    if config_json.startswith("{"):
        import json
        try:
            cfg = json.loads(config_json)
            # A Kotatsu-style or custom JSON schema:
            # {
            #   "item": ".chapter-item",
            #   "link": "a",
            #   "title": ".chapter-title",
            #   "url_attr": "href",
            #   "number_regex": ""
            # }
            # Or if it defines "list_selector", "item_selector", etc.
            item_sel = cfg.get("item") or cfg.get("item_selector") or expr
            link_sel = cfg.get("link") or cfg.get("link_selector")
            title_sel = cfg.get("title") or cfg.get("title_selector")
            url_attr = cfg.get("url_attr") or rule.url_attr or "href"
            num_re = cfg.get("number_regex") or rule.number_regex or ""
            get_first = cfg.get("get_first", rule.get_first)

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select(item_sel)
            if not items:
                return None, "", "json-no-items"
            el = items[0] if get_first else items[-1]
            
            # extract text for chapter number
            text_el = el.select_one(title_sel) if title_sel else el
            text = text_el.get_text(" ", strip=True) if text_el else el.get_text(" ", strip=True)
            chapter_num = _extract_number(text, num_re)

            # link
            href = ""
            link_el = el.select_one(link_sel) if link_sel else el
            if link_el:
                href = link_el.get(url_attr) or ""
            if not href and link_el != el:
                # Try finding any anchor inside link_el or el
                a = link_el.find("a", href=True) if hasattr(link_el, "find") else None
                if not a:
                    a = el.find("a", href=True)
                if a:
                    href = a.get("href", "")
            ch_url = _abs_url(base_url, href) if href else ""
            return chapter_num, ch_url, "json-kotatsu"
        except Exception as e:
            return None, "", f"json-error:{str(e)[:120]}"

    try:
        if mode == "xpath":
            from lxml import html as lxml_html  # requires lxml
            doc = lxml_html.fromstring(html)
            nodes = doc.xpath(expr)
            if not nodes:
                return None, "", "no-match"
            node = nodes[0] if rule.get_first else nodes[-1]
            # node can be element or string
            if hasattr(node, "text_content"):
                text = node.text_content().strip()
                attr = ""
                try:
                    attr = node.get(rule.url_attr or "href") or ""
                except Exception:
                    attr = ""
            else:
                text = str(node).strip()
                attr = ""
            chapter_num = _extract_number(text, rule.number_regex)
            ch_url = _abs_url(base_url, attr) if attr else ""
            return chapter_num, ch_url, "xpath"

        # css (BeautifulSoup)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(expr)
        if not matches:
            return None, "", "no-match"
        el = matches[0] if rule.get_first else matches[-1]
        text = el.get_text(" ", strip=True)
        chapter_num = _extract_number(text, rule.number_regex)
        # link
        href = ""
        if rule.url_attr:
            href = el.get(rule.url_attr) or ""
        if not href:
            a = el.find("a", href=True)
            if a:
                href = a.get("href", "")
        ch_url = _abs_url(base_url, href) if href else ""
        return chapter_num, ch_url, "css"
    except Exception as e:
        return None, "", f"error:{str(e)[:120]}"


def parse_all_chapters_from_html(html: str, base_url: str, rule: CustomSelectorRule) -> dict[float, str]:
    """
    Returns a dict of {chapter_num: chapter_url} from custom selectors/configurations.
    """
    out: dict[float, str] = {}
    selector = (rule.selector or "").strip()
    if not selector and not rule.raw_config:
        return out

    mode = "css"
    expr = selector
    if selector.startswith("css:"):
        mode, expr = "css", selector[4:].strip()
    elif selector.startswith("xpath:"):
        mode, expr = "xpath", selector[6:].strip()

    config_json = (rule.raw_config or "").strip()
    if not config_json and selector.startswith("{"):
        config_json = selector

    # Support parsing Kotatsu-style or dynamic raw selector configurations if it is JSON
    if config_json.startswith("{"):
        import json
        try:
            cfg = json.loads(config_json)
            item_sel = cfg.get("item") or cfg.get("item_selector") or expr
            link_sel = cfg.get("link") or cfg.get("link_selector")
            title_sel = cfg.get("title") or cfg.get("title_selector")
            url_attr = cfg.get("url_attr") or rule.url_attr or "href"
            num_re = cfg.get("number_regex") or rule.number_regex or ""

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select(item_sel)
            for el in items:
                # extract text for chapter number
                text_el = el.select_one(title_sel) if title_sel else el
                text = text_el.get_text(" ", strip=True) if text_el else el.get_text(" ", strip=True)
                chapter_num = _extract_number(text, num_re)
                if chapter_num is None:
                    continue

                # link
                href = ""
                link_el = el.select_one(link_sel) if link_sel else el
                if link_el:
                    href = link_el.get(url_attr) or ""
                if not href and link_el != el:
                    a = link_el.find("a", href=True) if hasattr(link_el, "find") else None
                    if not a:
                        a = el.find("a", href=True)
                    if a:
                        href = a.get("href", "")
                if href:
                    out[chapter_num] = _abs_url(base_url, href)
            return out
        except Exception:
            return out

    try:
        if mode == "xpath":
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(html)
            nodes = doc.xpath(expr)
            for node in nodes:
                if hasattr(node, "text_content"):
                    text = node.text_content().strip()
                    attr = ""
                    try:
                        attr = node.get(rule.url_attr or "href") or ""
                    except Exception:
                        attr = ""
                else:
                    text = str(node).strip()
                    attr = ""
                chapter_num = _extract_number(text, rule.number_regex)
                if chapter_num is not None and attr:
                    out[chapter_num] = _abs_url(base_url, attr)
            return out

        # css (BeautifulSoup)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(expr)
        for el in matches:
            text = el.get_text(" ", strip=True)
            chapter_num = _extract_number(text, rule.number_regex)
            if chapter_num is None:
                continue
            href = ""
            if rule.url_attr:
                href = el.get(rule.url_attr) or ""
            if not href:
                a = el.find("a", href=True)
                if a:
                    href = a.get("href", "")
            if href:
                out[chapter_num] = _abs_url(base_url, href)
        return out
    except Exception:
        return out

