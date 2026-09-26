"""事件存储：契约校验、版本递增、业务幂等、争议隔离与 JSONL 持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .contracts import validate_event
from .errors import ContractViolation, EventConflict
from .model import canonical

# 争议比对时忽略的字段：编号相同但其余关键内容不同即视为冲突。
_CONFLICT_IGNORED_KEYS = {"occurred_at", "version", "payload"}


class EventStore:
    """只追加的事件日志。

    - 追加前按交换契约校验信封；
    - version 由存储按聚合当前长度分配（从 1 递增），调用方给错即拒绝；
    - event_id 完全一致的重复提交直接返回已存事件（幂等）；
    - event_id 相同但事件类型、聚合或业务关键载荷不同的提交转入争议，
      不写入日志，并抛出 EventConflict。
    """

    def __init__(
        self,
        path: Path | str,
        schema: Mapping[str, Any],
        *,
        conflict_payload_keys: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self._path = Path(path)
        self._schema = schema
        self._disputes_path = self._path.with_suffix(self._path.suffix + ".disputes.json")
        self._conflict_payload_keys = {
            event_type: tuple(keys) for event_type, keys in (conflict_payload_keys or {}).items()
        }
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._disputes: list[dict[str, Any]] = []
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    event = json.loads(line)
                    self._events.append(event)
                    self._by_id[event["event_id"]] = event
        if self._disputes_path.exists():
            self._disputes = json.loads(self._disputes_path.read_text(encoding="utf-8"))

    def _persist_event(self, event: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def _persist_disputes(self) -> None:
        self._disputes_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._disputes_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._disputes, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self._disputes_path)

    # ---------- 追加 ----------

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        issues = validate_event(event, self._schema)
        if issues:
            summary = "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
            raise ContractViolation(f"事件不满足交换契约: {summary}")
        record = json.loads(canonical(event))  # 深拷贝为纯 JSON 结构
        event_id = record["event_id"]
        existing = self._by_id.get(event_id)
        if existing is not None:
            # 完全一致，或业务关键内容一致（仅元数据抖动）的重试都幂等返回已存事件。
            if canonical(existing) == canonical(record) or canonical(
                self._conflict_view(existing)
            ) == canonical(self._conflict_view(record)):
                return existing
            self._record_dispute("event_id", event_id, existing, record)
            raise EventConflict(f"事件编号 {event_id} 已被不同内容占用，已转入争议")
        aggregate_id = record["aggregate_id"]
        expected = 1 + sum(1 for item in self._events if item["aggregate_id"] == aggregate_id)
        if record["version"] != expected:
            raise ContractViolation(
                f"聚合 {aggregate_id} 的版本应为 {expected}，收到 {record['version']}"
            )
        self._persist_event(record)
        self._events.append(record)
        self._by_id[event_id] = record
        return record

    # ---------- 争议 ----------

    def _conflict_view(self, event: Mapping[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in event.items() if key not in _CONFLICT_IGNORED_KEYS}
        payload = event.get("payload", {})
        keys = self._conflict_payload_keys.get(str(event.get("event_type")), ())
        view["payload"] = {key: payload.get(key) for key in keys}
        return view

    def _record_dispute(
        self, kind: str, key: str, existing: Mapping[str, Any], incoming: Mapping[str, Any]
    ) -> None:
        def view(item: Mapping[str, Any]) -> dict[str, Any]:
            if "event_type" in item:
                return self._conflict_view(item)
            return dict(item)

        self._disputes.append(
            {"kind": kind, "key": key, "existing": view(existing), "incoming": view(incoming)}
        )
        self._persist_disputes()

    def record_external_dispute(
        self, kind: str, key: str, existing: Mapping[str, Any], incoming: Mapping[str, Any]
    ) -> None:
        """登记非事件类冲突（如撤展回执编号冲突）。"""
        self._record_dispute(kind, key, existing, incoming)

    def disputes(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._disputes]

    # ---------- 读取 ----------

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def replay(self, apply: Callable[[dict[str, Any]], None]) -> None:
        for event in self._events:
            apply(event)
