import asyncio
import pytest
import database
from services.worker_sync import sync_custom_data_to_worker


def test_get_valid_cover_url():
    # Test valid HTTP and HTTPS URLs
    valid_http = "http://example.com/cover.jpg"
    valid_https = "https://example.com/cover.png"
    assert database.get_valid_cover_url(valid_http) == valid_http
    assert database.get_valid_cover_url(valid_https) == valid_https

    # Test invalid / empty / None URLs fallback to DEFAULT_COVER_IMAGE_URL
    assert database.get_valid_cover_url(None) == database.DEFAULT_COVER_IMAGE_URL
    assert database.get_valid_cover_url("") == database.DEFAULT_COVER_IMAGE_URL
    assert database.get_valid_cover_url("   ") == database.DEFAULT_COVER_IMAGE_URL
    assert database.get_valid_cover_url("invalid_url_string") == database.DEFAULT_COVER_IMAGE_URL
    assert database.get_valid_cover_url(123) == database.DEFAULT_COVER_IMAGE_URL


def test_sqlite_pragmas_and_timeout(tmp_path):
    async def _test():
        db_file = str(tmp_path / "test_pragmas.db")
        orig_path = database.DB_PATH
        database.DB_PATH = db_file
        database._DB_CONN = None

        try:
            conn = await database._get_db()
            # Verify PRAGMA values
            async with conn.execute("PRAGMA journal_mode") as c:
                row = await c.fetchone()
                assert row[0].lower() == "wal"

            async with conn.execute("PRAGMA busy_timeout") as c:
                row = await c.fetchone()
                assert row[0] == 5000

            async with conn.execute("PRAGMA foreign_keys") as c:
                row = await c.fetchone()
                assert row[0] == 1
        finally:
            await database.close_db()
            database.DB_PATH = orig_path

    asyncio.run(_test())


def test_async_write_queue(tmp_path):
    async def _test():
        db_file = str(tmp_path / "test_queue.db")
        orig_path = database.DB_PATH
        database.DB_PATH = db_file
        database._DB_CONN = None

        try:
            await database.init_db()

            # Execute concurrent write operations via execute_write_async
            async def do_write(idx):
                await database.execute_write_async(
                    "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
                    (f"key_{idx}", f"val_{idx}")
                )

            tasks = [asyncio.create_task(do_write(i)) for i in range(20)]
            await asyncio.gather(*tasks)

            # Verify all 20 writes succeeded
            db = await database._get_db()
            async with db.execute("SELECT COUNT(*) FROM bot_settings") as cursor:
                row = await cursor.fetchone()
                assert row[0] == 20
        finally:
            await database.close_db()
            database.DB_PATH = orig_path

    asyncio.run(_test())


def test_sync_custom_data_to_worker_parallel():
    async def _test():
        class DummyRemoteDown:
            def __init__(self):
                self.is_enabled = True
                self.synced = False
                self.synced_data = None

            async def sync_custom_data(self, custom_sites, site_auth, custom_selectors):
                self.synced = True
                self.synced_data = (custom_sites, site_auth, custom_selectors)
                return {"status": "ok"}

        class DummyBot:
            def __init__(self):
                self.remote_down = DummyRemoteDown()

        class DummyDB:
            async def get_custom_sites(self):
                await asyncio.sleep(0.01)
                return [("domain1.com", "madara", 1, "now", "")]

            async def get_all_site_auth_data(self):
                await asyncio.sleep(0.01)
                return {"domain1.com": {"cookie": "abc"}}

            async def get_custom_selector_rules(self):
                await asyncio.sleep(0.01)
                return [("domain1.com", ".ch-item", "href", "", 1, 0, "", "")]

        bot = DummyBot()
        db = DummyDB()

        await sync_custom_data_to_worker(bot, db)

        assert bot.remote_down.synced is True
        custom_sites, site_auth, custom_selectors = bot.remote_down.synced_data
        assert custom_sites == {"madara": ["domain1.com"], "arabic": [], "generic": []}
        assert site_auth == {"domain1.com": {"cookie": "abc"}}
        assert "domain1.com" in custom_selectors
        assert custom_selectors["domain1.com"]["selector"] == ".ch-item"

    asyncio.run(_test())
