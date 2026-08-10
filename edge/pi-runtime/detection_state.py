from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal


Action = Literal["none", "confirmed", "evidence", "cleared"]


@dataclass(frozen=True)
class OccupancyDecision:
    action: Action
    detection: dict[str, Any] | None = None


def _distance_meters(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float:
    if a is None or b is None:
        return 0.0
    lat_a, lng_a = a
    lat_b, lng_b = b
    radius = 6_371_000.0
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = math.radians(lat_b - lat_a)
    d_lng = math.radians(lng_b - lng_a)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lng / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


class OccupancyState:
    """N-of-M temporal confirmation plus evidence and stable clearing.

    A single frame never creates or clears an incident. Repeated detections in
    a bounded inference window confirm occupancy; an active incident emits
    evidence periodically, then clears only after consecutive misses and a
    minimum clear duration. The cooldown is spatial, so a moving patrol can
    create a new incident immediately after travelling beyond the dedup radius.
    """

    def __init__(
        self,
        *,
        window_frames: int = 8,
        required_hits: int = 5,
        min_confirm_duration_sec: float = 2.0,
        clear_miss_frames: int = 5,
        clear_duration_sec: float = 3.0,
        evidence_interval_sec: float = 30.0,
        cooldown_sec: float = 20.0,
        spatial_dedup_meters: float = 15.0,
    ) -> None:
        if window_frames < 1 or not 1 <= required_hits <= window_frames:
            raise ValueError("required_hits must be between 1 and window_frames")
        self.window = deque(maxlen=window_frames)
        self.required_hits = required_hits
        self.min_confirm_duration_sec = max(0.0, min_confirm_duration_sec)
        self.clear_miss_frames = max(1, clear_miss_frames)
        self.clear_duration_sec = max(0.0, clear_duration_sec)
        self.evidence_interval_sec = max(1.0, evidence_interval_sec)
        self.cooldown_sec = max(0.0, cooldown_sec)
        self.spatial_dedup_meters = max(0.0, spatial_dedup_meters)
        self.active = False
        self.miss_streak = 0
        self.last_seen_at = 0.0
        self.last_evidence_at = 0.0
        self.last_closed_at = -1e30
        self.last_location: tuple[float, float] | None = None
        self.best_detection: dict[str, Any] | None = None

    @property
    def hit_count(self) -> int:
        return sum(1 for _, hit in self.window if hit)

    def update(
        self,
        *,
        now: float,
        detection: dict[str, Any] | None,
        location: tuple[float, float] | None = None,
    ) -> OccupancyDecision:
        hit = detection is not None
        self.window.append((now, hit))
        if hit and (self.best_detection is None or float(detection.get("score", 0)) >= float(self.best_detection.get("score", 0))):
            self.best_detection = dict(detection)

        if self.active:
            if hit:
                self.miss_streak = 0
                self.last_seen_at = now
                if now - self.last_evidence_at >= self.evidence_interval_sec:
                    self.last_evidence_at = now
                    return OccupancyDecision("evidence", dict(detection))
                return OccupancyDecision("none")
            self.miss_streak += 1
            if self.miss_streak >= self.clear_miss_frames and now - self.last_seen_at >= self.clear_duration_sec:
                self.active = False
                self.last_closed_at = now
                self.window.clear()
                self.miss_streak = 0
                self.best_detection = None
                return OccupancyDecision("cleared")
            return OccupancyDecision("none")

        if not hit:
            self.best_detection = None if self.hit_count == 0 else self.best_detection
            return OccupancyDecision("none")

        same_place_cooldown = (
            now - self.last_closed_at < self.cooldown_sec
            and _distance_meters(self.last_location, location) <= self.spatial_dedup_meters
        )
        hit_times = [timestamp for timestamp, matched in self.window if matched]
        enough_duration = bool(hit_times) and now - hit_times[0] >= self.min_confirm_duration_sec
        if self.hit_count >= self.required_hits and enough_duration and not same_place_cooldown:
            self.active = True
            self.last_seen_at = now
            self.last_evidence_at = now
            self.last_location = location
            confirmed = dict(self.best_detection or detection)
            self.window.clear()
            return OccupancyDecision("confirmed", confirmed)
        return OccupancyDecision("none")
