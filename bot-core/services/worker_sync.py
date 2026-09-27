from __future__ import annotations
import asyncio


async def sync_custom_data_to_worker(bot_instance, database_module) -> None:
    if not hasattr(bot_instance, "remote_down") or not bot_instance.remote_down.is_enabled:
        return
    try:
        sites, site_auth_raw, selectors_rows = await asyncio.gather(
            database_module.get_custom_sites(),
            database_module.get_all_site_auth_data(),
            database_module.get_custom_selector_rules(),
        )
        custom_sites = {
            "madara": [d[0] for d in sites if d[1] == "madara"],
            "arabic": [d[0] for d in sites if d[1] == "arabic"],
            "generic": [d[0] for d in sites if d[1] == "generic"],
        }
        site_auth = {d: a for d, a in site_auth_raw.items() if a}
        # custom selectors
        custom_selectors = {}
        for row in selectors_rows:
            d = row[0]
            sel = row[1]
            url_attr = row[2] if len(row) > 2 else "href"
            num_re = row[3] if len(row) > 3 else ""
            get_first = row[4] if len(row) > 4 else 0
            use_browser = row[5] if len(row) > 5 else 0
            notes = row[6] if len(row) > 6 else ""
            custom_selectors[d] = {
                "selector": sel,
                "url_attr": url_attr or "href",
                "number_regex": num_re or "",
                "get_first": bool(get_first),
                "use_browser": bool(use_browser),
                "notes": notes or "",
            }

        res = await bot_instance.remote_down.sync_custom_data(custom_sites, site_auth, custom_selectors)
        print(f"[Sync] Worker sync result: {res}")
    except Exception as e:
        print(f"[Sync] Failed to sync custom data to worker: {e}")

