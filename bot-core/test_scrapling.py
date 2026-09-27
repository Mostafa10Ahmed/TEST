import sys
import os
import time
import json

TEMP_DIR = r"D:\03_Apps_Games_Misc\v2\V3\discordbotprojectzip-mainzip-main\deploy_packages\mangasystem_bot_core\temp_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

from scrapling.fetchers import StealthyFetcher

def test_chapter_list():
    print("Fetching comic page using StealthyFetcher...")
    try:
        # StealthyFetcher returns a Response object that behaves similarly
        page = StealthyFetcher.fetch("https://comix.to/title/d1890-green-skin")
        
        with open(os.path.join(TEMP_DIR, "stealth_page.html"), "w", encoding="utf-8") as f:
            f.write(page.text)
            
        print("Page fetched. Length:", len(page.text))
        
        import re
        if "turnstile" in page.text.lower() or "cloudflare" in page.text.lower():
            print("Still hit Cloudflare Turnstile!")
        else:
            print("Bypassed Cloudflare successfully!")
            
    except Exception as e:
        print("Error in comic page:", e)
    
def test_chapter_images():
    print("Fetching chapter 7...")
    try:
        page = DynamicFetcher.fetch("https://comix.to/title/d1890-green-skin/10234334-chapter-7", headless=False, wait_until="domcontentloaded")
        time.sleep(8)
        
        # Extract images from window.env
        script_tags = page.css('script')
        env_text = ""
        for tag in script_tags:
            if tag.text and 'window.env' in tag.text:
                env_text = tag.text
                break
                
        if env_text:
            print("Found window.env!")
            try:
                # Poor man's extract
                import re
                match = re.search(r'window\.env\s*=\s*(\{.*?\});', env_text, re.DOTALL)
                if match:
                    env_data = json.loads(match.group(1))
                    images = env_data.get('imageUrls', [])
                    print(f"Found {len(images)} images!")
                    
                    # Download first 5 images as test
                    for i, img_url in enumerate(images):
                        if i >= 5: break # just download 5 to not waste time
                        print(f"Downloading image {i}...")
                        req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req) as response, open(os.path.join(TEMP_DIR, f"ch7_{i}.jpg"), 'wb') as f:
                            f.write(response.read())
            except Exception as e:
                print("Error parsing window.env:", e)
        else:
            print("Could not find window.env")
            
    except Exception as e:
        print("Error in chapter 7:", e)

if __name__ == "__main__":
    test_chapter_list()
    test_chapter_images()
