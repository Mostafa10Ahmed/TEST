import asyncio
import json
import os
import re
import urllib.request
from bs4 import BeautifulSoup

TEMP_DIR = r"D:\03_Apps_Games_Misc\v2\V3\discordbotprojectzip-mainzip-main\deploy_packages\mangasystem_bot_core\temp_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

async def download_chapter():
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://comix.to/title/d1890-green-skin/10794963-chapter-10"
    print(f"Target URL: {url}")
    
    match = re.search(r'/title/([^/]+)/(\d+)-([^/]+)', url)
    if not match:
        print("Invalid chapter URL format. Expected: https://comix.to/title/series-slug/chapter-id-chapter-slug")
        return
        
    series_slug = match.group(1)
    chapter_id = match.group(2)
    chapter_slug = match.group(3)
    
    print(f"Series Slug: {series_slug}")
    print(f"Chapter ID: {chapter_id}")
    print(f"Chapter Slug: {chapter_slug}")
    
    ch_dir = os.path.join(TEMP_DIR, f"{series_slug}_{chapter_id}_{chapter_slug}")
    os.makedirs(ch_dir, exist_ok=True)
    
    print("Starting download script for comix.to...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()
        
        # 1. Fetch Series Page for Cover & Publishers
        print("Navigating to series page...")
        series_url = f"https://comix.to/title/{series_slug}"
        await page.goto(series_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(5)
        
        # Click Turnstile if present
        checkbox = await page.query_selector('iframe[src*="turnstile"]')
        if checkbox:
            print("Turnstile detected, clicking...")
            await checkbox.click()
            await asyncio.sleep(5)
            
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        # Extract Cover Image
        cover_img = soup.find("img", class_=lambda x: x and "object-cover" in x)
        if cover_img and cover_img.get("src"):
            cover_url = cover_img["src"]
            print(f"Found Cover: {cover_url}")
            try:
                req = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp, open(os.path.join(ch_dir, "cover.jpg"), 'wb') as f:
                    f.write(resp.read())
            except Exception as e:
                print("Failed to download cover:", e)
                
        # Extract Publishers
        print("\nExtracting Publishers...")
        for group in soup.select("div.group"):
            text = group.get_text(strip=True)
            if text:
                print("Publisher/Group:", text)
                
        # 2. Fetch Chapter
        print(f"\nNavigating to {url}...")
        env_url_holder = []
        async def handle_request(request):
            if "/dist/env-" in request.url or "/env-" in request.url:
                env_url_holder.append(request.url)
        page.on("request", handle_request)
        
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(5)
        
        checkbox = await page.query_selector('iframe[src*="turnstile"]')
        if checkbox:
            print("Turnstile detected on chapter page, clicking...")
            await checkbox.click()
            await asyncio.sleep(5)
            
        # Extract images using env.b
        for _ in range(50):
            if env_url_holder: break
            await asyncio.sleep(0.1)
            
        if env_url_holder:
            env_url = env_url_holder[0]
            print(f"Captured env URL: {env_url}. Fetching chapter pages...")
            
            result = await page.evaluate("""async ({ envUrl, chId }) => {
                try {
                    const env = await import(envUrl);
                    const res = await env.b.get(`/chapters/` + chId);
                    return { ok: true, data: res };
                } catch (e) {
                    return { ok: false, error: String(e) };
                }
            }""", {"envUrl": env_url, "chId": chapter_id})
            
            if result.get("ok"):
                pages = result["data"].get("pages", {})
                base = pages.get("baseUrl", "")
                items = pages.get("items", [])
                
                images = []
                for item in items:
                    url_img = item.get("url", "")
                    if not url_img.startswith("http") and base:
                        url_img = base.rstrip("/") + "/" + url_img.lstrip("/")
                    if url_img.startswith("http"):
                        images.append(url_img)
                        
                print(f"Found {len(images)} images! Downloading...")
                for i, img_url in enumerate(images):
                    print(f"Downloading {i+1}/{len(images)}: {img_url}")
                    try:
                        req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req) as resp, open(os.path.join(ch_dir, f"{i:03d}.jpg"), 'wb') as f:
                            f.write(resp.read())
                    except Exception as e:
                        print(f"Failed to download image {i}: {e}")
            else:
                print("Failed to evaluate env.b:", result.get("error"))
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(download_chapter())
