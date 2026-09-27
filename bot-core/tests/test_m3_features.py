import asyncio
import pytest
import time
import database
from providers.base_provider import (
    USER_AGENT_POOL,
    get_random_user_agent,
    is_cloudflare_challenge,
    is_retryable_response,
    BaseProvider,
)
from providers.manager import ProviderManager, get_provider_health_matrix
from services.metrics import record_provider_check, ProviderHealthStats
from services.tracker_engine_v3 import is_main_chapter, should_ignore_chapter


def test_user_agent_pool_and_rotation():
    assert len(USER_AGENT_POOL) >= 5
    ua1 = get_random_user_agent()
    assert isinstance(ua1, str)
    assert len(ua1) > 20
    assert ua1 in USER_AGENT_POOL


def test_retryable_and_cloudflare_detection():
    assert is_retryable_response(429) is True
    assert is_retryable_response(502) is True
    assert is_retryable_response(503) is True
    assert is_retryable_response(200) is False

    # Cloudflare challenge checks
    cf_html = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"
    assert is_cloudflare_challenge(403, cf_html) is True
    assert is_cloudflare_challenge(200, cf_html) is True
    assert is_retryable_response(403, cf_html) is True

    normal_html = "<html><body><h1>Welcome to Manga Site</h1></body></html>"
    assert is_cloudflare_challenge(200, normal_html) is False
    assert is_retryable_response(200, normal_html) is False


def test_custom_chapter_filtering():
    # Main chapters
    assert is_main_chapter(1) is True
    assert is_main_chapter(10.0) is True
    assert is_main_chapter("100") is True
    assert is_main_chapter(0) is True

    # Fractional sub-chapters
    assert is_main_chapter(10.5) is False
    assert is_main_chapter(12.1) is False
    assert is_main_chapter("10.2") is False
    assert is_main_chapter(None) is False
    assert is_main_chapter("invalid") is False

    # should_ignore_chapter
    assert should_ignore_chapter(10.5, ignore_sub_chapters=True) is True
    assert should_ignore_chapter(12.1, ignore_sub_chapters=True) is True
    assert should_ignore_chapter(10.0, ignore_sub_chapters=True) is False
    assert should_ignore_chapter(10.5, ignore_sub_chapters=False) is False
    assert should_ignore_chapter(10.0, ignore_sub_chapters=False) is False


def test_provider_health_matrix():
    record_provider_check("TestProviderA", success=True, response_time_ms=120.0)
    record_provider_check("TestProviderA", success=True, response_time_ms=180.0)
    record_provider_check("TestProviderB", success=False, response_time_ms=500.0)
    record_provider_check("TestProviderB", success=False, response_time_ms=600.0)
    record_provider_check("TestProviderB", success=False, response_time_ms=700.0)

    matrix = get_provider_health_matrix()
    assert "TestProviderA" in matrix
    assert matrix["TestProviderA"]["status"] == "ONLINE"
    assert matrix["TestProviderA"]["successful_checks"] == 2
    assert matrix["TestProviderA"]["success_rate"] == 100.0

    assert "TestProviderB" in matrix
    assert matrix["TestProviderB"]["status"] == "OFFLINE"
    assert matrix["TestProviderB"]["failure_count"] == 3
    assert matrix["TestProviderB"]["success_rate"] == 0.0


def test_database_ignore_sub_chapters(tmp_path):
    async def _test():
        db_file = str(tmp_path / "test_subch.db")
        orig_path = database.DB_PATH
        database.DB_PATH = db_file
        database._DB_CONN = None

        try:
            await database.init_db()

            tid = await database.sv3_add(
                guild_id=123,
                url="https://example.com/manga/test",
                notification_channel_id="999",
                title="Test Manga",
                ignore_sub_chapters=1,
            )
            assert tid is not None

            tracker = await database.sv3_get(tid, 123)
            assert tracker is not None
            assert tracker["ignore_sub_chapters"] == 1

            # Update setting
            await database.sv3_update(tid, 123, ignore_sub_chapters=0)
            tracker_updated = await database.sv3_get(tid, 123)
            assert tracker_updated["ignore_sub_chapters"] == 0

        finally:
            await database.close_db()
            database.DB_PATH = orig_path

    asyncio.run(_test())


def test_multi_source_fallback_engine():
    async def _test():
        pm = ProviderManager()

        # Mock primary failure on custom domain and recovery via MangaDex
        class MockPrimaryFailProvider:
            async def get_all_chapters(self, url):
                return {}

        class MockFallbackProvider:
            async def get_all_chapters(self, url):
                return {100.0: "https://mangadex.org/chapter/100", 101.0: "https://mangadex.org/chapter/101"}

        pm.mangadex = MockFallbackProvider()

        async def mock_search(title, limit=3):
            return [{"title": title, "url": "https://mangadex.org/title/mock123"}]

        pm.search_manga = mock_search

        chs = await pm.get_all_chapters_with_fallback("https://unknownsite.com/manga/test-manga", series_title="Test Manga")
        assert 101.0 in chs
        assert chs[101.0] == "https://mangadex.org/chapter/101"

    asyncio.run(_test())
