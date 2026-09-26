"""展陈授权履约服务：命令、查询与派生义务。

规则要点：
- 涉及活态传承的说明与扩权须由权利人本人确认，商务角色不得代替；
- 锁场（EVENT_FROZEN）冻结当晚实际使用版本，之后该夜晚不再接受使用登记与展位确认；
- 撤回只影响生效时点之后的使用：之后的使用生成撤展案件，之前的保留证据并生成说明义务；
- 撤展回执按编号幂等，编号相同但素材或范围不同进入争议；
- 同一实体展位同一夜晚只确认一个方案；
- 全部状态由事件日志重放重建，重启后待办事项不丢失。
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import (
    BoothAlreadyConfirmed,
    DomainError,
    HeritageNoteRequired,
    LicenseNotUsable,
    PermissionDenied,
    ReceiptConflict,
    ReleaseFrozen,
    UnknownAsset,
    UnknownRemovalCase,
)
from .model import (
    ROLE_BUSINESS,
    Actor,
    Decision,
    Matter,
    Scope,
    canonical,
    night_of,
    now_utc,
    parse_time,
)
from .store import EventStore

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

# 各事件类型参与“编号相同但内容不同”判定的业务关键载荷字段。
_CONFLICT_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "LICENSE_GRANTED": ("asset", "partner_id", "right_holder", "scope"),
    "USE_RECORDED": ("asset_id", "partner_id", "channel", "use_time", "program_version", "space", "kind"),
    "EVENT_FROZEN": ("release_version", "night"),
    "LICENSE_WITHDRAWN": ("effective_at", "reason"),
    "REMOVAL_CONFIRMED": ("receipt_no", "case_id", "asset_id", "scope"),
}


class LicensingService:
    """节庆展陈授权履约簿的上层服务。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.assets: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.uses: dict[str, dict[str, Any]] = {}
        self.freezes: dict[str, dict[str, Any]] = {}
        self.withdrawals: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.removal_cases: dict[str, dict[str, Any]] = {}
        self.obligations: dict[str, dict[str, Any]] = {}
        self.booth_confirmed: dict[tuple[str, str], str] = {}
        self._frozen_nights: set[str] = set()
        self._agg_versions: dict[str, int] = {}
        self._applied: set[str] = set()
        self.store.replay(self._apply)

    @classmethod
    def open(cls, directory: Path | str, schema: Mapping[str, Any] | None = None) -> "LicensingService":
        """打开（或创建）一个持久化服务目录；重启后状态由事件日志重建。"""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if schema is None:
            schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        store = EventStore(
            directory / "events.jsonl", schema, conflict_payload_keys=_CONFLICT_PAYLOAD_KEYS
        )
        return cls(store)

    # ------------------------------------------------------------------
    # 事件应用
    # ------------------------------------------------------------------

    def _apply(self, event: dict[str, Any]) -> None:
        if event["event_id"] in self._applied:
            return
        self._applied.add(event["event_id"])
        event_type = event["event_type"]
        payload = event["payload"]
        self._agg_versions[event["aggregate_id"]] = event["version"]
        if event_type == "LICENSE_GRANTED":
            self._apply_grant(event, payload)
        elif event_type == "USE_RECORDED":
            self._apply_use(event, payload)
        elif event_type == "EVENT_FROZEN":
            self._apply_freeze(event, payload)
        elif event_type == "LICENSE_WITHDRAWN":
            self._apply_withdrawal(event, payload)
        elif event_type == "REMOVAL_CONFIRMED":
            self._apply_removal(event, payload)

    def _apply_grant(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        asset = dict(payload["asset"])
        self.assets.setdefault(asset["asset_id"], asset)
        self.grants[payload["grant_id"]] = {
            "grant_id": payload["grant_id"],
            "asset_id": asset["asset_id"],
            "partner_id": payload["partner_id"],
            "right_holder": payload["right_holder"],
            "scope": dict(payload["scope"]),
            "holder_confirmed": bool(payload.get("holder_confirmed")),
            "granted_by": dict(payload["granted_by"]),
            "granted_at": event["occurred_at"],
            "event_id": event["event_id"],
        }

    def _apply_use(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        use = {
            "use_id": payload["use_id"],
            "asset_id": payload["asset_id"],
            "partner_id": payload["partner_id"],
            "channel": payload["channel"],
            "program_version": payload.get("program_version"),
            "space": payload.get("space"),
            "promotion_ref": payload.get("promotion_ref"),
            "kind": payload.get("kind", "use"),
            "status": payload.get("status", "recorded"),
            "plan_id": payload.get("plan_id"),
            "planned": bool(payload.get("planned")),
            "use_time": payload["use_time"],
            "night": payload["night"],
            "grant_id": payload.get("grant_id"),
            "heritage_note": payload.get("heritage_note"),
            "recorded_by": dict(payload["recorded_by"]),
            "recorded_at": event["occurred_at"],
            "event_id": event["event_id"],
        }
        self.uses[use["use_id"]] = use
        if use["kind"] == "booth_booking" and use["status"] == "confirmed":
            self.booth_confirmed[(use["space"], use["night"])] = use["use_id"]
        self._derive_use(use, event["occurred_at"])

    def _apply_freeze(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        self.freezes[payload["release_id"]] = {
            "release_id": payload["release_id"],
            "night": payload["night"],
            "release_version": payload["release_version"],
            "frozen_at": payload["frozen_at"],
            "frozen_by": dict(payload["frozen_by"]),
            "snapshot": list(payload.get("snapshot", [])),
            "event_id": event["event_id"],
        }
        self._frozen_nights.add(payload["night"])

    def _apply_withdrawal(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        asset_id = event["aggregate_id"]
        self.withdrawals[asset_id] = {
            "asset_id": asset_id,
            "effective_at": payload["effective_at"],
            "reason": payload["reason"],
            "withdrew_by": dict(payload["withdrew_by"]),
            "withdrawn_at": event["occurred_at"],
            "event_id": event["event_id"],
        }
        for use in self.uses.values():
            if use["asset_id"] == asset_id:
                self._derive_use(use, event["occurred_at"])

    def _apply_removal(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        self.receipts[payload["receipt_no"]] = {
            "receipt_no": payload["receipt_no"],
            "case_id": event["aggregate_id"],
            "asset_id": payload["asset_id"],
            "partner_id": payload["partner_id"],
            "scope": payload.get("scope"),
            "confirmed_by": dict(payload["confirmed_by"]),
            "confirmed_at": event["occurred_at"],
            "event_id": event["event_id"],
        }
        case = self.removal_cases.get(event["aggregate_id"])
        if case is not None:
            case["status"] = "confirmed"
            case["receipt_no"] = payload["receipt_no"]

    # ------------------------------------------------------------------
    # 派生：撤展案件、说明义务、孤儿露出承诺
    # ------------------------------------------------------------------

    def _ensure_case(
        self, case_id: str, use: Mapping[str, Any], kind: str, reason: str, at: str
    ) -> None:
        if case_id not in self.removal_cases:
            self.removal_cases[case_id] = {
                "case_id": case_id,
                "use_id": use["use_id"],
                "asset_id": use["asset_id"],
                "partner_id": use["partner_id"],
                "channel": use["channel"],
                "night": use["night"],
                "kind": kind,
                "reason": reason,
                "status": "open",
                "created_at": at,
                "receipt_no": None,
            }

    def _ensure_obligation(
        self,
        obligation_id: str,
        use: Mapping[str, Any],
        kind: str,
        reason: str,
        at: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if obligation_id not in self.obligations:
            self.obligations[obligation_id] = {
                "obligation_id": obligation_id,
                "use_id": use["use_id"],
                "asset_id": use["asset_id"],
                "partner_id": use["partner_id"],
                "channel": use["channel"],
                "night": use["night"],
                "kind": kind,
                "reason": reason,
                "created_at": at,
                "extra": dict(extra or {}),
            }

    def _derive_use(self, use: Mapping[str, Any], at: str) -> None:
        withdrawal = self.withdrawals.get(use["asset_id"])
        if withdrawal is not None:
            if parse_time(use["use_time"]) >= parse_time(withdrawal["effective_at"]):
                self._ensure_case(
                    f"RC-{use['use_id']}", use, "withdrawal_removal", withdrawal["reason"], at
                )
            else:
                self._ensure_obligation(
                    f"OB-{use['use_id']}", use, "withdrawal_explanation", withdrawal["reason"], at
                )
        # 换节目：同一场次同一位置同一渠道出现新版本后，旧版本的露出承诺失去对象。
        if use["planned"] and use["night"] not in self._frozen_nights:
            for other in self.uses.values():
                if other["use_id"] == use["use_id"] or not other["planned"]:
                    continue
                same_slot = (
                    other["asset_id"],
                    other["channel"],
                    other["space"],
                    other["night"],
                ) == (use["asset_id"], use["channel"], use["space"], use["night"])
                if same_slot and other["program_version"] != use["program_version"]:
                    pair = sorted((use, other), key=lambda item: (item["use_time"], item["use_id"]))
                    earlier, later = pair[0], pair[1]
                    self._ensure_obligation(
                        f"OBX-{earlier['use_id']}",
                        earlier,
                        "orphan_commitment",
                        f"节目版本被 {later['program_version']} 替换，原露出承诺失去对应对象",
                        at,
                        extra={"replaced_by_use_id": later["use_id"]},
                    )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        occurred_at: datetime | None,
        event_id: str | None,
    ) -> dict[str, Any]:
        event = {
            "event_id": event_id or f"E-{len(self.store.events()) + 1:05d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": (occurred_at or now_utc()).isoformat(),
            "version": self._agg_versions.get(aggregate_id, 0) + 1,
            "payload": payload,
        }
        record = self.store.append(event)
        self._apply(record)
        return record

    @staticmethod
    def _envelope_covers(previous_scope: Mapping[str, Any], new_scope: Scope) -> bool:
        previous = Scope.from_payload(previous_scope)
        return (
            set(previous.channels) >= set(new_scope.channels)
            and previous.valid_from <= new_scope.valid_from
            and previous.valid_to >= new_scope.valid_to
        )

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def grant_license(
        self,
        *,
        asset_id: str,
        title: str,
        origin: str,
        right_holder: str,
        partner_id: str,
        channels: Sequence[str],
        valid_from: datetime,
        valid_to: datetime,
        actor: Actor,
        living_heritage: bool = False,
        heritage_note: str | None = None,
        holder_confirmed: bool = False,
        grant_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记素材来源并授予许可；活态传承素材的扩权须权利人本人确认。

        幂等重试需携带相同的 event_id 与业务标识（grant_id、occurred_at）。
        """
        if holder_confirmed and not actor.is_right_holder:
            raise PermissionDenied("许可确认只能由权利人本人作出")
        existing = self.assets.get(asset_id)
        if existing is not None:
            if existing["living_heritage"] != living_heritage:
                raise DomainError("素材的活态传承属性与已登记信息不一致")
            if existing["right_holder"] != right_holder:
                raise DomainError("素材的权利人与已登记信息不一致")
        scope = Scope(tuple(channels), valid_from, valid_to)
        if living_heritage:
            previous = [g for g in self.grants.values() if g["asset_id"] == asset_id]
            widening = not previous or not any(
                self._envelope_covers(g["scope"], scope) for g in previous
            )
            if widening:
                if not actor.is_right_holder:
                    raise PermissionDenied("商务人员不能代替权利人扩大活态传承许可")
                if not holder_confirmed:
                    raise PermissionDenied("扩大活态传承许可须经权利人确认")
        grant_id = grant_id or f"G-{len(self.grants) + 1:04d}"
        if event_id is None and grant_id in self.grants:
            return deepcopy(self.grants[grant_id])
        payload = {
            "grant_id": grant_id,
            "asset": {
                "asset_id": asset_id,
                "title": title,
                "origin": origin,
                "right_holder": right_holder,
                "living_heritage": living_heritage,
                "heritage_note": heritage_note,
            },
            "partner_id": partner_id,
            "right_holder": right_holder,
            "scope": scope.as_dict(),
            "holder_confirmed": holder_confirmed,
            "granted_by": actor.as_dict(),
        }
        self._emit("LICENSE_GRANTED", "asset_license", asset_id, payload, occurred_at, event_id)
        return deepcopy(self.grants[payload["grant_id"]])

    def record_use(
        self,
        *,
        asset_id: str,
        partner_id: str,
        channel: str,
        use_time: datetime,
        actor: Actor,
        program_version: str | None = None,
        space: str | None = None,
        promotion_ref: str | None = None,
        planned: bool = False,
        heritage_note: str | None = None,
        night: str | None = None,
        use_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记一次素材使用（实际或计划）；记录时授权必须覆盖该渠道与时点。"""
        asset = self.assets.get(asset_id)
        if asset is None:
            raise UnknownAsset(f"素材尚未登记: {asset_id}")
        night = night or night_of(use_time)
        if night in self._frozen_nights:
            raise ReleaseFrozen(f"{night} 已锁场，当晚实际使用版本以冻结快照为准")
        decision = self.can_use(asset_id, partner_id, channel, as_of=use_time)
        if not decision.allowed:
            raise LicenseNotUsable(f"登记使用时授权不可用: {decision.reason}")
        if asset["living_heritage"] and not (heritage_note and heritage_note.strip()):
            raise HeritageNoteRequired("涉及活态传承的使用必须附带经权利人确认的说明")
        use_id = use_id or f"U-{len(self.uses) + 1:04d}"
        if event_id is None and use_id in self.uses:
            return deepcopy(self.uses[use_id])
        payload = {
            "use_id": use_id,
            "asset_id": asset_id,
            "partner_id": partner_id,
            "channel": channel,
            "program_version": program_version,
            "space": space,
            "promotion_ref": promotion_ref,
            "kind": "use",
            "planned": planned,
            "use_time": use_time.isoformat(),
            "night": night,
            "grant_id": decision.grant_id,
            "heritage_note": heritage_note,
            "recorded_by": actor.as_dict(),
        }
        self._emit("USE_RECORDED", "partner_use", use_id, payload, occurred_at, event_id)
        return deepcopy(self.uses[use_id])

    def confirm_booth(
        self,
        *,
        asset_id: str,
        partner_id: str,
        booth: str,
        night: str,
        plan_id: str,
        use_time: datetime,
        actor: Actor,
        channel: str = "exhibit",
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """确认实体展位预订；同一展位同一夜晚只确认一个方案。"""
        if night in self._frozen_nights:
            raise ReleaseFrozen(f"{night} 已锁场，展位方案以冻结快照为准")
        if asset_id not in self.assets:
            raise UnknownAsset(f"素材尚未登记: {asset_id}")
        key = (booth, night)
        existing_id = self.booth_confirmed.get(key)
        if existing_id is not None:
            existing = self.uses[existing_id]
            if existing["plan_id"] == plan_id and existing["partner_id"] == partner_id:
                return deepcopy(existing)
            raise BoothAlreadyConfirmed(
                f"展位 {booth} 在 {night} 已被方案 {existing['plan_id']} 确认"
            )
        use_id = f"U-{len(self.uses) + 1:04d}"
        payload = {
            "use_id": use_id,
            "asset_id": asset_id,
            "partner_id": partner_id,
            "channel": channel,
            "program_version": plan_id,
            "space": booth,
            "promotion_ref": None,
            "kind": "booth_booking",
            "status": "confirmed",
            "plan_id": plan_id,
            "planned": True,
            "use_time": use_time.isoformat(),
            "night": night,
            "grant_id": None,
            "heritage_note": None,
            "recorded_by": actor.as_dict(),
        }
        self._emit("USE_RECORDED", "partner_use", use_id, payload, occurred_at, event_id)
        return deepcopy(self.uses[use_id])

    def freeze_release(
        self,
        *,
        night: str,
        release_version: str,
        actor: Actor,
        release_id: str | None = None,
        frozen_at: datetime | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """锁场：冻结当晚实际使用与已确认展位的版本快照。"""
        if night in self._frozen_nights:
            raise ReleaseFrozen(f"{night} 已锁场，不能重复冻结")
        release_id = release_id or f"F-{len(self.freezes) + 1:04d}"
        frozen_at = frozen_at or now_utc()
        snapshot = [
            {
                "use_id": use["use_id"],
                "asset_id": use["asset_id"],
                "partner_id": use["partner_id"],
                "channel": use["channel"],
                "program_version": use["program_version"],
                "space": use["space"],
                "use_time": use["use_time"],
                "recorded_by": dict(use["recorded_by"]),
            }
            for use in self.uses.values()
            if use["night"] == night and (not use["planned"] or use["kind"] == "booth_booking")
        ]
        snapshot.sort(key=lambda item: item["use_id"])
        payload = {
            "release_id": release_id,
            "night": night,
            "release_version": release_version,
            "frozen_at": frozen_at.isoformat(),
            "frozen_by": actor.as_dict(),
            "snapshot": snapshot,
        }
        self._emit("EVENT_FROZEN", "event_release", release_id, payload, occurred_at, event_id)
        return deepcopy(self.freezes[release_id])

    def withdraw_license(
        self,
        *,
        asset_id: str,
        effective_at: datetime,
        reason: str,
        actor: Actor,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """撤回许可：只影响生效时点之后的使用，之前的使用保留证据并生成说明义务。"""
        if actor.role == ROLE_BUSINESS:
            raise PermissionDenied("商务人员不能发起许可撤回")
        if asset_id not in self.assets:
            raise UnknownAsset(f"素材尚未登记: {asset_id}")
        existing = self.withdrawals.get(asset_id)
        if existing is not None:
            return deepcopy(existing)
        payload = {
            "effective_at": effective_at.isoformat(),
            "reason": reason,
            "withdrew_by": actor.as_dict(),
        }
        self._emit("LICENSE_WITHDRAWN", "asset_license", asset_id, payload, occurred_at, event_id)
        return deepcopy(self.withdrawals[asset_id])

    def confirm_removal(
        self,
        *,
        receipt_no: str,
        case_id: str,
        confirmed_by: Actor,
        asset_id: str | None = None,
        scope: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记撤下回执；编号幂等，编号相同但素材或范围不同进入争议。"""
        if receipt_no in self.receipts:
            receipt = self.receipts[receipt_no]
            same = (
                receipt["case_id"] == case_id
                and (asset_id is None or receipt["asset_id"] == asset_id)
                and (scope is None or canonical(receipt["scope"]) == canonical(scope))
            )
            if same:
                return deepcopy(receipt)
            incoming = {
                "receipt_no": receipt_no,
                "case_id": case_id,
                "asset_id": asset_id,
                "scope": scope,
            }
            self.store.record_external_dispute("removal_receipt", receipt_no, receipt, incoming)
            raise ReceiptConflict(f"回执编号 {receipt_no} 已对应其他素材或范围，已转入争议")
        case = self.removal_cases.get(case_id)
        if case is None:
            raise UnknownRemovalCase(f"撤展案件不存在: {case_id}")
        if case["status"] == "confirmed":
            raise DomainError(f"撤展案件 {case_id} 已结案，不能重复计数")
        payload = {
            "receipt_no": receipt_no,
            "case_id": case_id,
            "asset_id": case["asset_id"],
            "partner_id": case["partner_id"],
            "scope": dict(scope) if scope is not None else None,
            "confirmed_by": confirmed_by.as_dict(),
        }
        self._emit("REMOVAL_CONFIRMED", "removal_case", case_id, payload, occurred_at, event_id)
        return deepcopy(self.receipts[receipt_no])

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def can_use(
        self,
        asset_id: str,
        partner_id: str,
        channel: str,
        as_of: datetime | None = None,
    ) -> Decision:
        """回答某合作方当前能否把某素材用于指定渠道。"""
        as_of = as_of or now_utc()
        asset = self.assets.get(asset_id)
        if asset is None:
            return Decision(False, "asset_unknown", detail="素材尚未登记来源")
        withdrawal = self.withdrawals.get(asset_id)
        if withdrawal is not None and as_of >= parse_time(withdrawal["effective_at"]):
            return Decision(False, "withdrawn", detail=withdrawal["reason"])
        grants = [
            g
            for g in self.grants.values()
            if g["asset_id"] == asset_id and g["partner_id"] == partner_id
        ]
        if not grants:
            return Decision(False, "no_grant", detail="该合作方未获得此素材授权")
        covering = [g for g in grants if self._scope_covers(g["scope"], channel, as_of)]
        if not covering:
            latest = max(grants, key=lambda g: g["granted_at"])
            scope = Scope.from_payload(latest["scope"])
            if not scope.covers_channel(channel):
                return Decision(False, "channel_not_in_scope", latest["grant_id"], "渠道不在许可范围内")
            return Decision(False, "outside_term", latest["grant_id"], "当前时点不在许可期限内")
        grant = max(covering, key=lambda g: g["granted_at"])
        # 商务缩窄产生的未确认新授权，不遮蔽权利人此前已确认、范围仍覆盖的授权。
        if asset["living_heritage"] and not any(g["holder_confirmed"] for g in covering):
            return Decision(
                False, "heritage_unconfirmed", grant["grant_id"], "活态传承说明未经权利人确认"
            )
        confirmed = next((g for g in covering if g["holder_confirmed"]), grant)
        return Decision(True, "ok", confirmed["grant_id"])

    @staticmethod
    def _scope_covers(scope_payload: Mapping[str, Any], channel: str, moment: datetime) -> bool:
        scope = Scope.from_payload(scope_payload)
        return scope.covers_channel(channel) and scope.covers_moment(moment)

    def pending_matters(self, as_of: datetime | None = None) -> list[Matter]:
        """重启后仍需跟进的事项：未完成的撤展、说明义务与授权到期。"""
        as_of = as_of or now_utc()
        matters: list[Matter] = []
        for case in self.removal_cases.values():
            if case["status"] == "open":
                matters.append(
                    Matter(
                        "removal_open",
                        case["case_id"],
                        f"{case['night']} {case['channel']} 的使用待撤下：{case['reason']}",
                        extra={
                            "asset_id": case["asset_id"],
                            "use_id": case["use_id"],
                            "partner_id": case["partner_id"],
                        },
                    )
                )
        for obligation in self.obligations.values():
            matters.append(
                Matter(
                    "explanation_obligation",
                    obligation["obligation_id"],
                    obligation["reason"],
                    extra={
                        "kind": obligation["kind"],
                        "asset_id": obligation["asset_id"],
                        "use_id": obligation["use_id"],
                        "partner_id": obligation["partner_id"],
                    },
                )
            )
        for grant in self.grants.values():
            scope = Scope.from_payload(grant["scope"])
            if scope.valid_to < as_of and grant["asset_id"] not in self.withdrawals:
                # 已有同素材、同合作方的新授权覆盖当前时点时，不再报旧授权到期。
                renewed = any(
                    other["grant_id"] != grant["grant_id"]
                    and other["asset_id"] == grant["asset_id"]
                    and other["partner_id"] == grant["partner_id"]
                    and Scope.from_payload(other["scope"]).covers_moment(as_of)
                    for other in self.grants.values()
                )
                if renewed:
                    continue
                related = [u for u in self.uses.values() if u["grant_id"] == grant["grant_id"]]
                matters.append(
                    Matter(
                        "license_expired",
                        grant["grant_id"],
                        f"授权已于 {scope.valid_to.isoformat()} 到期，素材不得继续使用",
                        extra={
                            "asset_id": grant["asset_id"],
                            "partner_id": grant["partner_id"],
                            "use_count": len(related),
                        },
                    )
                )
        return sorted(matters, key=lambda matter: (matter.kind, matter.ref_id))

    def use_basis(self, use_id: str) -> dict[str, Any]:
        """还原一次使用的依据链与责任人。"""
        use = self.uses.get(use_id)
        if use is None:
            raise DomainError(f"使用记录不存在: {use_id}")
        asset = deepcopy(self.assets.get(use["asset_id"]))
        grant = deepcopy(self.grants.get(use["grant_id"])) if use["grant_id"] else None
        freeze = next(
            (deepcopy(f) for f in self.freezes.values() if f["night"] == use["night"]), None
        )
        withdrawal = deepcopy(self.withdrawals.get(use["asset_id"]))
        case = deepcopy(self.removal_cases.get(f"RC-{use_id}"))
        obligation = self.obligations.get(f"OB-{use_id}") or self.obligations.get(
            f"OBX-{use_id}"
        )
        receipt = None
        if case and case.get("receipt_no"):
            receipt = deepcopy(self.receipts.get(case["receipt_no"]))
        return {
            "use": deepcopy(use),
            "asset": asset,
            "grant": grant,
            "freeze": freeze,
            "withdrawal": withdrawal,
            "removal_case": case,
            "obligation": deepcopy(obligation) if obligation else None,
            "receipt": receipt,
        }

    def disputes(self) -> list[dict[str, Any]]:
        """编号冲突（事件或回执）转入的争议列表。"""
        return self.store.disputes()


def load_service(directory: Path | str) -> LicensingService:
    """便捷入口：等价于 LicensingService.open。"""
    return LicensingService.open(directory)


__all__ = ["LicensingService", "load_service", "DEFAULT_SCHEMA_PATH"]
