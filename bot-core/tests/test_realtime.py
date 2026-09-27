import pytest
import datetime
import json
from services.adaptive_poller import AdaptivePoller
from services.feed_watcher import FeedWatcher

def test_adaptive_poller_intervals():
    poller = AdaptivePoller()
    
    # Test auto tier intervals
    assert poller.get_effective_interval("auto", 90.0, "api") == 15    # Instant tier with API (min 15s)
    assert poller.get_effective_interval("auto", 90.0, "rss") == 30    # Instant tier with RSS (min 30s)
    assert poller.get_effective_interval("auto", 90.0, "scrape") == 60 # Instant tier with Scrape (min 60s)
    
    assert poller.get_effective_interval("auto", 70.0, "api") == 120   # Fast tier
    assert poller.get_effective_interval("auto", 40.0, "api") == 300   # Normal tier
    assert poller.get_effective_interval("auto", 20.0, "api") == 900   # Slow tier
    assert poller.get_effective_interval("auto", 5.0, "api") == 3600   # Idle tier

def test_adaptive_poller_heat_events():
    poller = AdaptivePoller()
    
    # Base heat updates
    assert poller.update_heat(50.0, "new_chapter") == 90.0
    assert poller.update_heat(50.0, "error") == 40.0
    assert poller.update_heat(50.0, "no_change") == 49.5
    assert poller.update_heat(50.0, "schedule_boost") == 85.0

def test_adaptive_poller_decay():
    poller = AdaptivePoller()
    now = datetime.datetime.now()
    
    # Test decay over 5 hours (2.0 score per hour -> 10.0 total decay)
    five_hours_ago = (now - datetime.timedelta(hours=5)).isoformat()
    assert poller.decay_heat_by_time(80.0, five_hours_ago, now) == 70.0

def test_adaptive_poller_schedule_learning():
    poller = AdaptivePoller()
    
    # Sample release: Thursday (weekday 3) at 17:00
    release = datetime.datetime(2026, 6, 11, 17, 30) # 2026-06-11 is Thursday
    
    pattern_json = None
    # Learn 3 times to build confidence
    for _ in range(3):
        pattern_json = poller.learn_schedule(pattern_json, release)
        
    pattern = json.loads(pattern_json)
    assert pattern["day"] == 3
    assert pattern["hour"] == 17
    assert pattern["confidence"] == 1.0

def test_feed_watcher_method_detection():
    watcher = FeedWatcher()
    
    assert watcher.detect_check_method("https://mangadex.org/title/uuid") == "mangadex_api"
    assert watcher.detect_check_method("https://comick.io/comic/slug") == "comick_api"
    assert watcher.detect_check_method("https://www.webtoons.com/en/fantasy/title") == "rss"
    # Now WordPress/Asura/Lekmanga default to rss via fallback detection
    assert watcher.detect_check_method("https://asuracomic.net/series/title") == "rss"
    assert watcher.detect_check_method("https://lekmanga.net/manga/title") == "rss"
    assert watcher.detect_check_method("https://generic-non-wp.com/series/title") == "scrape"


def test_feed_watcher_rss_parsing():
    watcher = FeedWatcher()
    
    xml_rss = """
    <rss version="2.0">
        <channel>
            <item>
                <title>Solo Leveling Chapter 150</title>
                <link>https://asuracomic.net/series/solo-leveling/chapter-150</link>
            </item>
            <item>
                <title>Solo Leveling Chapter 149</title>
                <link>https://asuracomic.net/series/solo-leveling/chapter-149</link>
            </item>
        </channel>
    </rss>
    """
    
    new_chapters = watcher.parse_rss_chapters(xml_rss, 149.0)
    assert len(new_chapters) == 1
    assert new_chapters[0]["num"] == 150.0
    assert new_chapters[0]["url"] == "https://asuracomic.net/series/solo-leveling/chapter-150"
