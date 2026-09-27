import asyncio
import json
import pytest
import database
import discord
from discord.ext import commands

from ui.tracker_containers_v3 import build_progress_bar, build_tracker_detail
from cogs.admin import build_site_provider_health_matrix, build_worker_performance_metrics
from ui.components_v2 import InteractiveHelpView


def test_progress_bar_builder():
    # Test 0%
    bar0 = build_progress_bar(0.0, 100.0)
    assert "`░░░░░░░░░░` **0%**" in bar0

    # Test 50%
    bar50 = build_progress_bar(50.0, 100.0)
    assert "50%" in bar50
    assert "█████░░░░░" in bar50

    # Test 100%
    bar100 = build_progress_bar(100.0, 100.0)
    assert "100%" in bar100
    assert "██████████" in bar100

    # Test cap at 100%
    bar_over = build_progress_bar(150.0, 100.0)
    assert "100%" in bar_over


def test_tracker_detail_container():
    tracker_data = {
        "id": 1,
        "url": "https://asurascans.com/manga/solo-leveling",
        "title": "Solo Leveling",
        "last_chapter": 179.0,
        "cover_url": "https://example.com/cover.jpg",
        "notification_channel_id": "123456789",
        "mention_role_id": "987654321",
        "auto_download": 1,
        "paused": 0,
        "created_at": "2026-07-24",
    }

    container = build_tracker_detail(tracker_data, sub_count=5)
    assert isinstance(container, discord.ui.Container)
    # Check structure
    children = container.children
    assert len(children) >= 2


def test_database_user_subscriptions_and_heal(tmp_path):
    async def _test():
        db_file = str(tmp_path / "test_m4.db")
        orig_path = database.DB_PATH
        database.DB_PATH = db_file
        database._DB_CONN = None

        try:
            await database.init_db()

            # Test toggle subscription
            tracker_id = 101
            user_1 = 5555
            user_2 = 6666

            sub1 = await database.toggle_user_subscription(tracker_id, user_1)
            assert sub1 is True  # Subscribed

            sub1_again = database.is_user_subscribed
            assert await sub1_again(tracker_id, user_1) is True

            sub2 = await database.toggle_user_subscription(tracker_id, user_2)
            assert sub2 is True

            subs = await database.get_series_subscribers(tracker_id)
            assert len(subs) == 2
            assert user_1 in subs
            assert user_2 in subs

            # Unsubscribe user_1
            unsub1 = await database.toggle_user_subscription(tracker_id, user_1)
            assert unsub1 is False
            assert await database.is_user_subscribed(tracker_id, user_1) is False

            subs_after = await database.get_series_subscribers(tracker_id)
            assert len(subs_after) == 1
            assert user_2 in subs_after

            # Test heal system
            heal_res = await database.heal_system()
            assert isinstance(heal_res, dict)
            assert heal_res["db_ok"] is True
            assert heal_res["db_check_result"].lower() == "ok"

        finally:
            await database.close_db()
            database.DB_PATH = orig_path

    asyncio.run(_test())


def test_admin_metrics_helpers():
    from services.metrics import record_provider_check
    record_provider_check("AsuraScans", success=True, response_time_ms=100.0)
    record_provider_check("AsuraScans", success=False, response_time_ms=300.0)

    class DummyBot:
        metrics = None
        remote_down = None

    bot = DummyBot()
    matrix = build_site_provider_health_matrix(bot)
    assert "MangaDex" in matrix
    assert "ONLINE" in matrix
    assert "AsuraScans" in matrix
    assert "50.0%" in matrix
    assert "200ms" in matrix

    perf = build_worker_performance_metrics(bot)
    assert "Status" in perf
    assert "Max Concurrent Workers" in perf


def test_command_tree_boundary_registration():
    async def _test():
        from main import setup_hook, bot
        try:
            await setup_hook()
        except commands.ExtensionAlreadyLoaded:
            pass
        cmds = bot.tree.get_commands()
        cmd_names = sorted([cmd.name for cmd in cmds])
        assert "admin" in cmd_names
        assert "help" in cmd_names
        assert "tracker" in cmd_names
        assert len(cmds) >= 3

    asyncio.run(_test())


def test_interactive_help_view():
    class DummyBot:
        pass

    help_view = InteractiveHelpView(DummyBot(), user_rank=3)
    assert help_view.current_tab == "user"
    
    help_view._build_sync_tab("vip")
    assert help_view.current_tab == "vip"

    help_view._build_sync_tab("admin")
    assert help_view.current_tab == "admin"
