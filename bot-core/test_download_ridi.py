import asyncio
import os
import sys

from providers.manager import ProviderManager
from manga_downloader import MangaDownloader

async def main():
    downloader = MangaDownloader()
    manager = downloader.provider_manager
    
    url = "https://ridibooks.com/books/5870000001/view"
    print(f"Testing download for Ridibooks: {url}")
    
    dest_folder = "temp_downloads/test_ridi_1"
    os.makedirs(dest_folder, exist_ok=True)
    
    try:
        images = await manager.get_images(url)
        print(f"Got {len(images)} images.")
        
        if not images:
            print("Failed to get images.")
            sys.exit(1)
            
        print("Starting download...")
        await downloader.download_chapter_to_folder(url, images, "세 개의 세계 1화", dest_folder)
        print("Download completed!")
        
        # Verify that files were downloaded
        files = os.listdir(dest_folder)
        print(f"Downloaded files in folder ({len(files)}):")
        for f in sorted(files)[:10]:
            print(f" - {f}")
            
        if len(files) > 0:
            print("SUCCESS: Chapter downloaded completely!")
        else:
            print("FAILED: No files downloaded!")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
