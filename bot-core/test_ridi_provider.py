import asyncio
import sys
from providers.raw_providers import RidibooksProvider

async def main():
    provider = RidibooksProvider()
    series_url = "https://ridibooks.com/books/5870000001"
    
    print("Testing get_chapters_with_lock_info...")
    chapters = await provider.get_chapters_with_lock_info(series_url)
    
    print(f"Total chapters extracted: {len(chapters)}")
    if not chapters:
        print("FAILED: No chapters extracted!")
        sys.exit(1)
        
    unlocked = [num for num, info in chapters.items() if not info["locked"]]
    locked = [num for num, info in chapters.items() if info["locked"]]
    
    print(f"Unlocked chapters ({len(unlocked)}): {unlocked}")
    print(f"Locked chapters ({len(locked)}): {locked[:10]} ... {locked[-5:] if len(locked) > 5 else ''}")
    
    if len(unlocked) == 0:
        print("FAILED: Expected some unlocked chapters!")
        sys.exit(1)
        
    # Pick the first chapter to test image download
    first_ch_url = chapters[unlocked[0]]["url"]
    print(f"\nTesting get_images for first unlocked chapter: {first_ch_url}")
    images = await provider.get_images(first_ch_url)
    print(f"Total images found: {len(images)}")
    if images:
        print(f"First image URL: {images[0][:120]}...")
        print("SUCCESS: Images extracted correctly!")
    else:
        print("FAILED: No images extracted!")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
