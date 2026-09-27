import asyncio
from mangasystem_bot_core.providers.asura_provider import AsuraProvider
from mangasystem_bot_core.providers.base_provider import SITE_AUTH, update_site_auth_cache
from mangasystem_bot_core.database import init_db, set_site_auth

async def main():
    await init_db()
    domain = "asurascans.com"
    cookies = {
        "__cf_vid": "7cd9ec262987d78ea43bb76a417ffac4",
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjo5NTAzODMsInVzZXJuYW1lIjoiTW9yZV9CbG9sIiwicm9sZSI6InByZW1pdW0iLCJwcmVtaXVtX3VudGlsIjoiMjAyNi0wNi0yNlQwMDowNzo0OFoiLCJpc3MiOiJhc3VyYS1hcGkiLCJleHAiOjE3ODAwMDc2MTEsIm5iZiI6MTc4MDAwNjcxMSwiaWF0IjoxNzgwMDA2NzExfQ.WjNABragmfk_3mcqtT9HGodh9RETcjT8zH2ghljcvzA",
        "cf_clearance": "wlGtryZfQxWP_01Gsshh5ChacBYCPFKpKYDHpjbFgPY-1774130748-1.2.1.1-TkdPZC9i_Pl8jopGDlRDwh04Ta9PVY7V84AcDBNqQBVoTRU0DqxgQ_QG4x9qdGEMFNrO_XJpqoYwifApzjQX8tAhaKyjIyOz8JDs.TBDQl9yUS5Lir0btOUMa067TqeImXvHuepRaBPfvyfVVUdBGwIaLx1TEBNRcKQszxLE7OTxJP5oox6glTpf7OP1Z10BMvNeZlBOb1tHte_m5DKOA77YMZOT23gDZJ7dsBRrtg8",
        "refresh_token": "4d68ec765de8a346bf1f7266a0e61eef86ff9ef68767bcca344f9f6ad705e30d"
    }
    
    update_site_auth_cache({domain: cookies})
    print(f"Set cookies for {domain}")
    
    p = AsuraProvider()
    url = "https://asurascans.com/comics/initializing-the-sect-system-7b57f74d/chapter/41"
    print(f"Fetching images for {url}...")
    imgs = await p.get_images(url)
    
    print(f"Found {len(imgs)} images.")
    if imgs:
        print(f"First 3 images: {imgs[:3]}")
    else:
        html = p.fetch_html(url)
        print("Failed to get images. HTML length:", len(html) if html else 0)
        if html:
            with open("asura_test.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("Wrote HTML to asura_test.html")

if __name__ == "__main__":
    asyncio.run(main())
