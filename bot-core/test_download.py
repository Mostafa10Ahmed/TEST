import asyncio
import os
import sys

from providers.manager import ProviderManager
from manga_downloader import MangaDownloader

async def main():
    manager = ProviderManager()
    downloader = MangaDownloader(manager)
    
    url = "https://comix.to/title/n8we-the-chick-class-hunter-is-filial/9897089-chapter-70"
    print(f"Testing download for: {url}")
    
    dest_folder = "temp_downloads/test_comix_70"
    os.makedirs(dest_folder, exist_ok=True)
    
    try:
        images = await manager.get_images(url)
        print(f"Got {len(images)} images.")
        for img in images[:5]:
            print(f" - {img}")
            
        if not images:
            print("Failed to get images.")
            return
            
        print("Starting download...")
        await downloader.download_chapter_to_folder(url, images, "Test Chapter 70", dest_folder)
        print("Download completed!")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
