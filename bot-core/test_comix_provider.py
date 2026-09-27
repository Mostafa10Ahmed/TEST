"""
اختبار شامل للـ ComixProvider المُصلح
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    from providers.comix_provider import ComixProvider
    
    provider = ComixProvider()
    
    SERIES_URL = "https://comix.to/title/n8we-the-chick-class-hunter-is-filial"
    CHAPTER_URL = "https://comix.to/title/n8we-the-chick-class-hunter-is-filial/9897089-chapter-70"
    
    print("=" * 60)
    print("TEST 1: get_all_chapters")
    print("=" * 60)
    chapters = await provider.get_all_chapters(SERIES_URL)
    print(f"Total chapters found: {len(chapters)}")
    if chapters:
        nums = sorted(chapters.keys())
        print(f"Chapter numbers: min={min(nums)}, max={max(nums)}")
        print(f"First 5: {nums[:5]}")
        print(f"Last 5: {nums[-5:]}")
        # Check if we got 72
        if max(nums) >= 72:
            print("[OK] Found latest chapter 72!")
        else:
            print(f"[WARN] Latest found: {max(nums)} (expected 72)")
    else:
        print("❌ No chapters found!")
    
    print("\n" + "=" * 60)
    print("TEST 2: get_images (Chapter 70)")
    print("=" * 60)
    images = await provider.get_images(CHAPTER_URL)
    print(f"Total images found: {len(images)}")
    if images:
        print("First 3 images:")
        for img in images[:3]:
            print(f"  {img}")
        print("✅ Images fetched successfully!")
    else:
        print("❌ No images found!")
    
    print("\n" + "=" * 60)
    print("TEST 3: get_latest_chapter")
    print("=" * 60)
    latest = provider.get_latest_chapter(SERIES_URL)
    print(f"Latest chapter: {latest}")
    if latest and latest >= 72:
        print("✅ Latest chapter correct!")
    else:
        print(f"⚠️ Latest: {latest} (expected >= 72)")

if __name__ == "__main__":
    asyncio.run(main())
