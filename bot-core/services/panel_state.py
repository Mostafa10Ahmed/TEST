from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta


@dataclass
class PanelState:
    url: str = ""
    title: str = "Manga_Chapter"
    destination: str = "Auto"
    series_url: str = ""
    selected_chapter: float | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PanelStateStore:
    def __init__(self) -> None:
        self._state: dict[int, PanelState] = {}
        self._ttl = timedelta(hours=6)

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [uid for uid, state in self._state.items() if now - state.updated_at > self._ttl]
        for uid in expired:
            self._state.pop(uid, None)

    def get(self, user_id: int) -> PanelState:
        self._cleanup()
        if user_id not in self._state:
            self._state[user_id] = PanelState()
        return self._state[user_id]

    def set_url(self, user_id: int, url: str) -> PanelState:
        state = self.get(user_id)
        state.url = url.strip()
        state.updated_at = datetime.now(timezone.utc)
        return state

    def set_title(self, user_id: int, title: str) -> PanelState:
        state = self.get(user_id)
        state.title = title.strip() or "Manga_Chapter"
        state.updated_at = datetime.now(timezone.utc)
        return state

    def set_destination(self, user_id: int, destination: str) -> PanelState:
        state = self.get(user_id)
        state.destination = destination
        state.updated_at = datetime.now(timezone.utc)
        return state

    def set_series_url(self, user_id: int, series_url: str) -> PanelState:
        state = self.get(user_id)
        state.series_url = series_url.strip()
        state.updated_at = datetime.now(timezone.utc)
        return state

    def set_selected_chapter(self, user_id: int, chapter: float, chapter_url: str) -> PanelState:
        state = self.get(user_id)
        state.selected_chapter = chapter
        state.url = chapter_url.strip()
        state.updated_at = datetime.now(timezone.utc)
        return state
