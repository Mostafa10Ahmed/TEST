from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass
class _MetricsData:
    search_ok: int = 0
    search_fail: int = 0
    download_ok: int = 0
    download_fail: int = 0
    stitch_ok: int = 0
    stitch_fail: int = 0
    download_duration_total_sec: float = 0.0
    download_duration_count: int = 0


class RuntimeMetrics:
    def __init__(self) -> None:
        self._data = _MetricsData()
        self._lock = Lock()

    def inc(self, field: str, amount: int = 1) -> None:
        with self._lock:
            current = getattr(self._data, field, 0)
            setattr(self._data, field, current + amount)

    def add_download_duration(self, seconds: float) -> None:
        with self._lock:
            self._data.download_duration_total_sec += max(0.0, seconds)
            self._data.download_duration_count += 1

    def snapshot(self) -> dict:
        with self._lock:
            d = self._data
            avg = (
                d.download_duration_total_sec / d.download_duration_count
                if d.download_duration_count > 0
                else 0.0
            )
            return {
                "search_ok": d.search_ok,
                "search_fail": d.search_fail,
                "download_ok": d.download_ok,
                "download_fail": d.download_fail,
                "stitch_ok": d.stitch_ok,
                "stitch_fail": d.stitch_fail,
                "download_avg_sec": round(avg, 2),
            }
