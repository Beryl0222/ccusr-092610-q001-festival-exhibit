"""领域模型：范围、时间、角色与只读视图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

CHANNELS: tuple[str, ...] = ("stage", "promotion", "merchandise", "exhibit", "broadcast")
CHANNEL_RANK: dict[str, int] = {name: index for index, name in enumerate(CHANNELS)}

ROLE_RIGHT_HOLDER = "right_holder"
ROLE_BUSINESS = "business"
ROLE_OPERATIONS = "operations"
ROLES: tuple[str, ...] = (ROLE_RIGHT_HOLDER, ROLE_BUSINESS, ROLE_OPERATIONS)


def parse_time(value: str) -> datetime:
    """解析契约时间；契约层已保证带时区。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return parsed


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def night_of(moment: datetime) -> str:
    """按本地（事件发生）日期归属一个场次夜晚。"""
    return moment.date().isoformat()


@dataclass(frozen=True)
class Actor:
    """操作者：角色决定能否代表权利人作出确认。"""

    actor_id: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未登记的角色: {self.role!r}")

    @property
    def is_right_holder(self) -> bool:
        return self.role == ROLE_RIGHT_HOLDER

    def as_dict(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "role": self.role}


@dataclass(frozen=True)
class Scope:
    """许可范围：渠道包络按 CHANNELS 顺序比较，期限按闭区间比较。"""

    channels: tuple[str, ...]
    valid_from: datetime
    valid_to: datetime

    def __post_init__(self) -> None:
        unknown = [name for name in self.channels if name not in CHANNEL_RANK]
        if unknown:
            raise ValueError(f"未登记的渠道: {unknown!r}")
        if self.valid_from > self.valid_to:
            raise ValueError("许可期限起点不能晚于终点")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Scope":
        return cls(
            channels=tuple(payload.get("channels", ())),
            valid_from=parse_time(str(payload["valid_from"])),
            valid_to=parse_time(str(payload["valid_to"])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
        }

    def envelope(self) -> tuple[int, datetime, datetime]:
        """渠道数、起点、终点构成的包络；逐项不减即为扩大。"""
        return (len(self.channels), self.valid_from, self.valid_to)

    def covers_channel(self, channel: str) -> bool:
        return channel in self.channels

    def covers_moment(self, moment: datetime) -> bool:
        return self.valid_from <= moment <= self.valid_to


@dataclass(frozen=True)
class Decision:
    """can_use 的判定结果。"""

    allowed: bool
    reason: str
    grant_id: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "grant_id": self.grant_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Matter:
    """重启后仍需跟进的事项。"""

    kind: str
    ref_id: str
    summary: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "summary": self.summary,
            "extra": dict(self.extra),
        }


def canonical(value: Any) -> str:
    """稳定排序的 JSON 表示，用于幂等比对。"""
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
