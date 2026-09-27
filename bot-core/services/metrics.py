from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Dict


@dataclass
class ProviderHealthStats:
    provider_name: str
    total_checks: int = 0
    successful_checks: int = 0
    failure_count: int = 0
    total_response_time_ms: float = 0.0
    last_response_time_ms: float = 0.0
    consecutive_failures: int = 0

    @property
    def avg_response_time_ms(self) -> float:
        if self.total_checks > 0:
            return round(self.total_response_time_ms / self.total_checks, 2)
        return 0.0

    @property
    def success_rate(self) -> float:
        if self.total_checks > 0:
            return round((self.successful_checks / self.total_checks) * 100, 2)
        return 100.0

    @property
    def status(self) -> str:
        if self.total_checks == 0:
            return "ONLINE"
        if self.consecutive_failures >= 3 or (self.total_checks >= 3 and self.successful_checks == 0):
            return "OFFLINE"
        if self.failure_count > 0 and (self.success_rate < 80.0 or self.consecutive_failures > 0 or self.avg_response_time_ms > 5000):
            return "DEGRADED"
        return "ONLINE"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider_name,
            "total_checks": self.total_checks,
            "successful_checks": self.successful_checks,
            "failure_count": self.failure_count,
            "response_time_ms": self.avg_response_time_ms,
            "success_rate": self.success_rate,
            "status": self.status,
        }


_HEALTH_STATS: Dict[str, ProviderHealthStats] = {}
_HEALTH_LOCK = Lock()


def record_provider_check(provider_name: str, success: bool, response_time_ms: float = 0.0) -> None:
    """Record health metrics for a provider check attempt."""
    with _HEALTH_LOCK:
        if provider_name not in _HEALTH_STATS:
            _HEALTH_STATS[provider_name] = ProviderHealthStats(provider_name=provider_name)
        stats = _HEALTH_STATS[provider_name]
        stats.total_checks += 1
        stats.total_response_time_ms += max(0.0, response_time_ms)
        stats.last_response_time_ms = round(response_time_ms, 2)
        if success:
            stats.successful_checks += 1
            stats.consecutive_failures = 0
        else:
            stats.failure_count += 1
            stats.consecutive_failures += 1


def get_provider_health_matrix() -> Dict[str, dict]:
    """Return structured health data matrix for display in /admin."""
    with _HEALTH_LOCK:
        return {name: stats.to_dict() for name, stats in _HEALTH_STATS.items()}


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
                "provider_health": get_provider_health_matrix(),
            }

