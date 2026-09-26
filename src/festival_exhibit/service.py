"""展陈授权履约服务。

在契约校验（contracts.validate_event）之上提供业务幂等、冲突隔离与状态推进：

- 授权授予与修订：活态传承说明须由权利人本人确认；非权利人（如商务人员）
  只能维持或收窄许可，不得扩大用途渠道或期限。
- 使用登记：校验合作方、渠道、期限与撤回状态，记录责任人与宣传引用。
- 锁场冻结：冻结当晚实际使用的节目版本；同一实体展位在同一活动日只能
  被一个方案确认，并行预订的其余方案进入冲突。
- 授权撤回：只影响生效时点之后尚未发生的使用；已完成的展示保留证据，
  并生成对合作方的后续说明义务（removal_case）。
- 撤下回执：同一合作方相同回执编号重复提交不重复计数；编号相同但素材
  或范围不同的提交进入争议，且不计数。
- 持久化：全部事件追加写入 JSONL 日志，服务重启后重放恢复，可列出未
  完成的撤展/说明义务与已到期的授权事项。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import validate_event

_AGGREGATE_OF_EVENT = {
    "LICENSE_GRANTED": "asset_license",
    "USE_RECORDED": "partner_use",
    "EVENT_FROZEN": "event_release",
    "LICENSE_WITHDRAWN": "asset_license",
    "REMOVAL_CONFIRMED": "removal_case",
}

_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

# 仓库内 schema 文件缺失时的兜底契约，与 contracts/domain.schema.json 保持一致。
_FALLBACK_SCHEMA: dict[str, Any] = {
    "required": [
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "version",
        "payload",
    ],
    "properties": {
        "event_type": {"enum": sorted(_AGGREGATE_OF_EVENT)},
        "aggregate_type": {"enum": ["asset_license", "event_release", "partner_use", "removal_case"]},
    },
    "payload_required_by_event": {
        "LICENSE_GRANTED": ["right_holder", "scope"],
        "EVENT_FROZEN": ["release_version", "frozen_at"],
        "LICENSE_WITHDRAWN": ["effective_at", "reason"],
    },
}


class ServiceError(Exception):
    """业务规则冲突，携带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _load_default_schema() -> Mapping[str, Any]:
    if _DEFAULT_SCHEMA_PATH.is_file():
        return json.loads(_DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _FALLBACK_SCHEMA


def _parse_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ServiceError("invalid_time", f"{field} 必须是带时区的 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ServiceError("invalid_time", f"{field} 不是合法的 ISO 时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError("invalid_time", f"{field} 必须携带时区")
    return parsed


def _normalize_channels(channels: Iterable[str]) -> list[str]:
    result = sorted({str(item).strip() for item in channels if str(item).strip()})
    if not result:
        raise ServiceError("empty_scope", "许可用途渠道不能为空")
    return result


def _normalize_scope_text(scope: Any) -> list[str]:
    """回执中的范围可为字符串或字符串列表，统一为排序后的列表再比较。"""
    if isinstance(scope, str):
        items: Iterable[Any] = [scope]
    elif isinstance(scope, (list, tuple)):
        items = scope
    else:
        raise ServiceError("invalid_scope", "回执范围必须是字符串或字符串列表")
    result = sorted({str(item).strip() for item in items if str(item).strip()})
    if not result:
        raise ServiceError("invalid_scope", "回执范围不能为空")
    return result


class LicenseService:
    """展陈授权履约服务，事件溯源 + JSONL 日志持久化。"""

    def __init__(self, journal_path: str | os.PathLike[str], schema: Mapping[str, Any] | None = None) -> None:
        self._journal_path = Path(journal_path)
        self._schema = schema if schema is not None else _load_default_schema()
        self._events: list[dict[str, Any]] = []
        self._event_index: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._licenses: dict[str, dict[str, Any]] = {}
        self._uses: dict[str, dict[str, Any]] = {}
        self._releases: dict[str, dict[str, Any]] = {}
        self._cases: dict[str, dict[str, Any]] = {}
        self._receipts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._disputes: list[dict[str, Any]] = []
        self._replay()

    # ------------------------------------------------------------------
    # 日志与投影
    # ------------------------------------------------------------------

    def _replay(self) -> None:
        if not self._journal_path.is_file():
            return
        for line_no, line in enumerate(self._journal_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            issues = validate_event(event, self._schema)
            if issues:
                raise ServiceError("journal_corrupt", f"日志第 {line_no} 行未通过契约校验: {issues[0].code}")
            self._apply(event)

    def _append_to_file(self, event: Mapping[str, Any]) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _retry(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """同一 event_id 的重试：业务内容一致（版本号除外）即幂等返回，否则冲突。"""
        existing = self._event_index.get(event["event_id"])
        if existing is None:
            return None
        strip = lambda item: {key: value for key, value in item.items() if key != "version"}
        if strip(existing) == strip(event):
            return {"event": existing, "idempotent": True}
        raise ServiceError("event_conflict", f"事件标识 {event['event_id']} 已被不同内容占用")

    def _commit(self, event: dict[str, Any]) -> dict[str, Any]:
        """校验并追加事件；相同 event_id 的重复提交幂等返回，内容不同则冲突。"""
        issues = validate_event(event, self._schema)
        if issues:
            first = issues[0]
            raise ServiceError("contract_violation", f"{first.field}: {first.message}")
        replayed = self._retry(event)
        if replayed is not None:
            return replayed
        self._append_to_file(event)
        self._apply(event)
        return {"event": event, "idempotent": False}

    def _next_version(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0) + 1

    def _apply(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        self._event_index[event["event_id"]] = event
        self._versions[event["aggregate_id"]] = event["version"]
        handler = getattr(self, f"_apply_{event['event_type'].lower()}")
        handler(event)

    def _apply_license_granted(self, event: dict[str, Any]) -> None:
        body = event["payload"]
        license_id = event["aggregate_id"]
        state = self._licenses.get(license_id)
        if state is None:
            state = {"events": [], "withdrawn": None}
            self._licenses[license_id] = state
        state.update(
            asset_id=body["asset_id"],
            source=body["source"],
            right_holder=body["right_holder"],
            channels=list(body["scope"]["channels"]),
            valid_from=body["scope"]["valid_from"],
            valid_to=body["scope"]["valid_to"],
            partners=list(body.get("partners", [])),
            heritage_note=body.get("heritage_note"),
            heritage_confirmed_by=body.get("heritage_confirmed_by"),
            version=event["version"],
        )
        state["events"].append(event["event_id"])

    def _apply_use_recorded(self, event: dict[str, Any]) -> None:
        body = event["payload"]
        self._uses[event["aggregate_id"]] = {
            "use_id": event["aggregate_id"],
            "partner_id": body["partner_id"],
            "license_id": body["license_id"],
            "license_version": body["license_version"],
            "asset_id": body["asset_id"],
            "channel": body["channel"],
            "use_time": body["use_time"],
            "actor": body["actor"],
            "release_id": body.get("release_id"),
            "release_version": body.get("release_version"),
            "position": body.get("position"),
            "promotion_refs": list(body.get("promotion_refs", [])),
            "event_id": event["event_id"],
        }

    def _apply_event_frozen(self, event: dict[str, Any]) -> None:
        body = event["payload"]
        self._releases[event["aggregate_id"]] = {
            "release_id": event["aggregate_id"],
            "event_day": body["event_day"],
            "positions": list(body.get("positions", [])),
            "release_version": body["release_version"],
            "frozen_at": body["frozen_at"],
            "items": list(body.get("items", [])),
            "actor": body.get("actor"),
            "status": "frozen",
            "event_id": event["event_id"],
        }

    def _apply_license_withdrawn(self, event: dict[str, Any]) -> None:
        body = event["payload"]
        license_id = event["aggregate_id"]
        license_state = self._licenses[license_id]
        license_state["withdrawn"] = {
            "effective_at": body["effective_at"],
            "reason": body["reason"],
            "actor": body.get("actor"),
            "event_id": event["event_id"],
        }
        license_state["events"].append(event["event_id"])
        # 已完成的展示保留证据，并按合作方生成后续说明义务。
        effective = _parse_instant(body["effective_at"], "payload.effective_at")
        completed_by_partner: dict[str, list[str]] = {}
        for use in self._uses.values():
            if use["license_id"] != license_id:
                continue
            if _parse_instant(use["use_time"], "payload.use_time") < effective:
                completed_by_partner.setdefault(use["partner_id"], []).append(use["use_id"])
        for partner_id, use_ids in sorted(completed_by_partner.items()):
            case_id = f"{license_id}:{partner_id}:explanation"
            case = self._cases.get(case_id)
            if case is None:
                self._cases[case_id] = {
                    "case_id": case_id,
                    "kind": "explanation",
                    "license_id": license_id,
                    "asset_id": license_state["asset_id"],
                    "partner_id": partner_id,
                    "use_ids": sorted(use_ids),
                    "reason": body["reason"],
                    "created_by_event": event["event_id"],
                    "status": "open",
                    "closed_by_event": None,
                }

    def _apply_removal_confirmed(self, event: dict[str, Any]) -> None:
        body = event["payload"]
        key = (body["partner_id"], body["receipt_no"])
        signature = {"asset_id": body["asset_id"], "scope": list(body["scope"])}
        submissions = self._receipts.setdefault(key, [])
        counted_before = [item for item in submissions if item["outcome"] == "counted"]
        if not counted_before:
            outcome = "counted"
        elif counted_before[0]["signature"] == signature:
            outcome = "duplicate"  # 重复提交相同回执，不重复计数
        else:
            outcome = "disputed"  # 编号相同但素材或范围不同，进入争议
        record = {
            "event_id": event["event_id"],
            "case_id": event["aggregate_id"],
            "partner_id": body["partner_id"],
            "receipt_no": body["receipt_no"],
            "signature": signature,
            "actor": body.get("actor"),
            "occurred_at": event["occurred_at"],
            "outcome": outcome,
        }
        submissions.append(record)
        if outcome == "disputed":
            self._disputes.append(
                {
                    "partner_id": body["partner_id"],
                    "receipt_no": body["receipt_no"],
                    "first_event_id": counted_before[0]["event_id"],
                    "conflicting_event_id": event["event_id"],
                    "first": counted_before[0]["signature"],
                    "conflicting": signature,
                }
            )
        if outcome == "counted":
            case = self._find_open_case(body["partner_id"], body["asset_id"])
            if case is not None:
                case["status"] = "closed"
                case["closed_by_event"] = event["event_id"]

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------

    def _find_open_case(self, partner_id: str, asset_id: str) -> dict[str, Any] | None:
        for case in self._cases.values():
            if case["status"] == "open" and case["partner_id"] == partner_id and case["asset_id"] == asset_id:
                return case
        return None

    def _find_case(self, partner_id: str, asset_id: str) -> dict[str, Any] | None:
        """优先返回未结案义务，其次返回已结案的，保证回执重试落在同一案卷上。"""
        open_case = self._find_open_case(partner_id, asset_id)
        if open_case is not None:
            return open_case
        for case in self._cases.values():
            if case["partner_id"] == partner_id and case["asset_id"] == asset_id:
                return case
        return None

    def _license_or_raise(self, license_id: str) -> dict[str, Any]:
        state = self._licenses.get(license_id)
        if state is None:
            raise ServiceError("license_not_found", f"授权 {license_id} 不存在")
        return state

    def _license_state_at(self, license_id: str, version: int) -> dict[str, Any] | None:
        """按授权聚合自身事件流还原指定版本时的许可范围。"""
        state: dict[str, Any] = {}
        for event in self._events:
            if event["aggregate_id"] != license_id or event["version"] > version:
                continue
            if event["event_type"] == "LICENSE_GRANTED":
                body = event["payload"]
                state.update(
                    asset_id=body["asset_id"],
                    right_holder=body["right_holder"],
                    channels=list(body["scope"]["channels"]),
                    valid_from=body["scope"]["valid_from"],
                    valid_to=body["scope"]["valid_to"],
                )
        return state or None

    def _use_status(self, use: Mapping[str, Any]) -> str:
        license_state = self._licenses.get(use["license_id"])
        if license_state and license_state["withdrawn"]:
            effective = _parse_instant(license_state["withdrawn"]["effective_at"], "payload.effective_at")
            if _parse_instant(use["use_time"], "payload.use_time") >= effective:
                return "cancelled"
            return "completed"
        return "recorded"

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def grant_license(
        self,
        *,
        event_id: str,
        occurred_at: str,
        license_id: str,
        asset_id: str,
        source: str,
        right_holder: str,
        channels: Iterable[str],
        valid_from: str,
        valid_to: str,
        actor: str,
        partners: Iterable[str] = (),
        heritage_note: str | None = None,
        heritage_confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        """授予或修订授权。再次调用同一 license_id 即视为修订。"""
        channel_list = _normalize_channels(channels)
        start = _parse_instant(valid_from, "valid_from")
        end = _parse_instant(valid_to, "valid_to")
        if not start < end:
            raise ServiceError("invalid_term", "许可期限的起点必须早于终点")
        payload: dict[str, Any] = {
            "asset_id": asset_id,
            "source": source,
            "right_holder": right_holder,
            "scope": {
                "channels": channel_list,
                "valid_from": valid_from,
                "valid_to": valid_to,
            },
            "partners": sorted({str(item) for item in partners}),
            "actor": actor,
        }
        if heritage_note is not None:
            payload["heritage_note"] = heritage_note
            payload["heritage_confirmed_by"] = heritage_confirmed_by
        event = {
            "event_id": event_id,
            "event_type": "LICENSE_GRANTED",
            "aggregate_type": "asset_license",
            "aggregate_id": license_id,
            "occurred_at": occurred_at,
            "version": self._next_version(license_id),
            "payload": payload,
        }
        replayed = self._retry(event)
        if replayed is not None:
            return replayed
        if heritage_note is not None and heritage_confirmed_by != right_holder:
            # 涉及活态传承的说明须由权利人本人确认，商务人员不得代为确认。
            raise ServiceError("heritage_confirmation_required", "活态传承说明须由权利人本人确认")
        existing = self._licenses.get(license_id)
        if existing is not None:
            if existing["right_holder"] != right_holder:
                raise ServiceError("holder_change_forbidden", "修订不得变更权利人")
            if actor != right_holder:
                # 非权利人修订只能维持或收窄：渠道不得增加，期限不得超出原范围。
                old_channels = set(existing["channels"])
                if not set(channel_list) <= old_channels:
                    raise ServiceError("scope_expansion_forbidden", "商务人员不能代替权利人扩大许可渠道")
                old_start = _parse_instant(existing["valid_from"], "valid_from")
                old_end = _parse_instant(existing["valid_to"], "valid_to")
                if start < old_start or end > old_end:
                    raise ServiceError("scope_expansion_forbidden", "商务人员不能代替权利人延长许可期限")
        return self._commit(event)

    def record_use(
        self,
        *,
        event_id: str,
        occurred_at: str,
        use_id: str,
        partner_id: str,
        license_id: str,
        channel: str,
        use_time: str,
        actor: str,
        release_id: str | None = None,
        release_version: int | None = None,
        position: str | None = None,
        promotion_refs: Iterable[str] = (),
    ) -> dict[str, Any]:
        """登记合作方的一次实际使用，记录责任人。"""
        license_state = self._license_or_raise(license_id)
        payload: dict[str, Any] = {
            "partner_id": partner_id,
            "license_id": license_id,
            "license_version": license_state["version"],
            "asset_id": license_state["asset_id"],
            "channel": channel,
            "use_time": use_time,
            "actor": actor,
            "promotion_refs": [str(item) for item in promotion_refs],
        }
        if release_id is not None:
            payload["release_id"] = release_id
        if release_version is not None:
            payload["release_version"] = release_version
        if position is not None:
            payload["position"] = position
        event = {
            "event_id": event_id,
            "event_type": "USE_RECORDED",
            "aggregate_type": "partner_use",
            "aggregate_id": use_id,
            "occurred_at": occurred_at,
            "version": 1,
            "payload": payload,
        }
        replayed = self._retry(event)
        if replayed is not None:
            return replayed
        existing_use = self._uses.get(use_id)
        if existing_use is not None:
            raise ServiceError("use_exists", f"使用记录 {use_id} 已存在")
        if license_state["partners"] and partner_id not in license_state["partners"]:
            raise ServiceError("partner_not_licensed", f"合作方 {partner_id} 不在授权范围内")
        if channel not in license_state["channels"]:
            raise ServiceError("channel_out_of_scope", f"渠道 {channel} 超出许可用途")
        use_at = _parse_instant(use_time, "use_time")
        if not _parse_instant(license_state["valid_from"], "valid_from") <= use_at <= _parse_instant(
            license_state["valid_to"], "valid_to"
        ):
            raise ServiceError("outside_license_term", "使用时间不在许可期限内")
        withdrawn = license_state["withdrawn"]
        if withdrawn and use_at >= _parse_instant(withdrawn["effective_at"], "payload.effective_at"):
            raise ServiceError("license_withdrawn", "授权已撤回，该时点之后的使用不被允许")
        return self._commit(event)

    def freeze_event(
        self,
        *,
        event_id: str,
        occurred_at: str,
        release_id: str,
        event_day: str,
        release_version: int,
        frozen_at: str,
        actor: str,
        positions: Iterable[str] = (),
        items: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """锁场并冻结当晚实际使用的节目版本；同一展位同一活动日只能确认一次。"""
        _parse_instant(frozen_at, "frozen_at")
        position_list = sorted({str(item).strip() for item in positions if str(item).strip()})
        event = {
            "event_id": event_id,
            "event_type": "EVENT_FROZEN",
            "aggregate_type": "event_release",
            "aggregate_id": release_id,
            "occurred_at": occurred_at,
            "version": self._next_version(release_id),
            "payload": {
                "event_day": event_day,
                "release_version": release_version,
                "frozen_at": frozen_at,
                "positions": position_list,
                "items": [dict(item) for item in items],
                "actor": actor,
            },
        }
        replayed = self._retry(event)
        if replayed is not None:
            return replayed
        if release_id in self._releases:
            raise ServiceError("release_frozen", f"方案 {release_id} 已冻结")
        for other in self._releases.values():
            if other["event_day"] != event_day:
                continue
            overlap = sorted(set(position_list) & set(other["positions"]))
            if overlap:
                raise ServiceError(
                    "position_conflict",
                    f"展位 {','.join(overlap)} 已被方案 {other['release_id']} 确认",
                )
        return self._commit(event)

    def withdraw_license(
        self,
        *,
        event_id: str,
        occurred_at: str,
        license_id: str,
        effective_at: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """撤回授权：只影响生效时点之后的使用，已完成的展示保留并生成说明义务。"""
        license_state = self._license_or_raise(license_id)
        _parse_instant(effective_at, "effective_at")
        event = {
            "event_id": event_id,
            "event_type": "LICENSE_WITHDRAWN",
            "aggregate_type": "asset_license",
            "aggregate_id": license_id,
            "occurred_at": occurred_at,
            "version": self._next_version(license_id),
            "payload": {"effective_at": effective_at, "reason": reason, "actor": actor},
        }
        replayed = self._retry(event)
        if replayed is not None:
            return replayed
        if license_state["withdrawn"] is not None:
            raise ServiceError("already_withdrawn", f"授权 {license_id} 已撤回")
        return self._commit(event)

    def submit_removal_receipt(
        self,
        *,
        event_id: str,
        occurred_at: str,
        partner_id: str,
        receipt_no: str,
        asset_id: str,
        scope: str | Iterable[str],
        actor: str,
    ) -> dict[str, Any]:
        """提交撤下回执：重复提交不重复计数，编号相同内容不同进入争议。"""
        scope_list = _normalize_scope_text(scope)
        if not receipt_no.strip():
            raise ServiceError("invalid_receipt", "回执编号不能为空")
        case = self._find_case(partner_id, asset_id)
        case_id = case["case_id"] if case is not None else f"receipt:{partner_id}:{receipt_no}"
        event = {
            "event_id": event_id,
            "event_type": "REMOVAL_CONFIRMED",
            "aggregate_type": "removal_case",
            "aggregate_id": case_id,
            "occurred_at": occurred_at,
            "version": self._next_version(case_id),
            "payload": {
                "partner_id": partner_id,
                "receipt_no": receipt_no,
                "asset_id": asset_id,
                "scope": scope_list,
                "actor": actor,
            },
        }
        result = self._commit(event)
        submissions = self._receipts[(partner_id, receipt_no)]
        result["outcome"] = submissions[-1]["outcome"]
        return result

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def can_use(self, *, partner_id: str, asset_id: str, channel: str, at: str) -> dict[str, Any]:
        """按合作方回答某素材当前能否用于指定渠道，并给出依据。"""
        moment = _parse_instant(at, "at")
        candidates = [
            (license_id, state)
            for license_id, state in sorted(self._licenses.items())
            if state["asset_id"] == asset_id and (not state["partners"] or partner_id in state["partners"])
        ]
        if not candidates:
            return {"allowed": False, "reasons": ["no_license"], "basis": None}
        best: dict[str, Any] | None = None
        for license_id, state in candidates:
            reasons: list[str] = []
            if channel not in state["channels"]:
                reasons.append("channel_out_of_scope")
            if not _parse_instant(state["valid_from"], "valid_from") <= moment <= _parse_instant(
                state["valid_to"], "valid_to"
            ):
                reasons.append("outside_license_term")
            withdrawn = state["withdrawn"]
            if withdrawn and moment >= _parse_instant(withdrawn["effective_at"], "payload.effective_at"):
                reasons.append("license_withdrawn")
            basis = {
                "license_id": license_id,
                "license_version": state["version"],
                "right_holder": state["right_holder"],
                "channels": list(state["channels"]),
                "valid_from": state["valid_from"],
                "valid_to": state["valid_to"],
                "withdrawn": withdrawn,
            }
            verdict = {"allowed": not reasons, "reasons": reasons, "basis": basis}
            if not reasons:
                return verdict
            if best is None:
                best = verdict
        return best

    def explain_use(self, use_id: str) -> dict[str, Any]:
        """还原一次使用的授权依据与责任人。"""
        use = self._uses.get(use_id)
        if use is None:
            raise ServiceError("use_not_found", f"使用记录 {use_id} 不存在")
        license_state = self._licenses[use["license_id"]]
        basis_at_version = self._license_state_at(use["license_id"], use["license_version"])
        case = self._find_case_for_use(use)
        return {
            "use": dict(use),
            "status": self._use_status(use),
            "responsible": use["actor"],
            "basis": {
                "license_id": use["license_id"],
                "license_version": use["license_version"],
                "right_holder": license_state["right_holder"],
                "source": license_state["source"],
                "scope_at_use": basis_at_version,
                "license_events": list(license_state["events"]),
            },
            "withdrawal": license_state["withdrawn"],
            "removal_case": dict(case) if case is not None else None,
        }

    def _find_case_for_use(self, use: Mapping[str, Any]) -> dict[str, Any] | None:
        for case in self._cases.values():
            if use["use_id"] in case["use_ids"]:
                return case
        return None

    def pending_removals(self) -> list[dict[str, Any]]:
        """尚未完成的撤展/说明义务。"""
        return [dict(case) for case in sorted(self._cases.values(), key=lambda c: c["case_id"]) if case["status"] == "open"]

    def expired_licenses(self, now: str) -> list[dict[str, Any]]:
        """指定期点已到期（或已撤回）的授权事项。"""
        moment = _parse_instant(now, "now")
        result = []
        for license_id, state in sorted(self._licenses.items()):
            expired = _parse_instant(state["valid_to"], "valid_to") <= moment
            if not expired and state["withdrawn"] is None:
                continue
            open_cases = [
                case["case_id"]
                for case in self._cases.values()
                if case["license_id"] == license_id and case["status"] == "open"
            ]
            result.append(
                {
                    "license_id": license_id,
                    "asset_id": state["asset_id"],
                    "right_holder": state["right_holder"],
                    "valid_to": state["valid_to"],
                    "expired": expired,
                    "withdrawn": state["withdrawn"],
                    "open_cases": sorted(open_cases),
                }
            )
        return result

    def disputes(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._disputes]

    def recovery_report(self, now: str) -> dict[str, Any]:
        """重启后的恢复视图：未完成撤展、到期授权与回执争议。"""
        return {
            "pending_removals": self.pending_removals(),
            "expired_licenses": self.expired_licenses(now),
            "disputes": self.disputes(),
        }
