"""展陈授权履约服务的业务规则测试。"""

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from festival_exhibit.errors import (  # noqa: E402
    BoothAlreadyConfirmed,
    EventConflict,
    HeritageNoteRequired,
    LicenseNotUsable,
    PermissionDenied,
    ReceiptConflict,
    ReleaseFrozen,
)
from festival_exhibit.model import Actor, now_utc  # noqa: E402
from festival_exhibit.service import LicensingService  # noqa: E402

HOLDER = Actor("holder-zigong", "right_holder")
BUSINESS = Actor("biz-hung-hom", "business")
OPS = Actor("ops-stage", "operations")
NIGHT = "2026-09-26"
T = now_utc()
DAY = timedelta(days=1)


def make_service() -> LicensingService:
    tmp = tempfile.mkdtemp()
    return LicensingService.open(Path(tmp))


def grant_lantern(service: LicensingService, **overrides):
    kwargs = dict(
        asset_id="A-lantern",
        title="缠枝莲纹自贡花灯",
        origin="自贡灯会传承人口授图样",
        right_holder="holder-zigong",
        partner_id="P-troupe",
        channels=("stage", "promotion"),
        valid_from=T - 30 * DAY,
        valid_to=T + 30 * DAY,
        actor=HOLDER,
        living_heritage=True,
        heritage_note="传统缠枝莲纹由传承人按祖制扎制",
        holder_confirmed=True,
    )
    kwargs.update(overrides)
    return service.grant_license(**kwargs)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    # ---------- 授权与角色 ----------

    def test_living_heritage_grant_requires_holder_confirmation(self) -> None:
        with self.assertRaises(PermissionDenied):
            grant_lantern(self.service, actor=BUSINESS)
        with self.assertRaises(PermissionDenied):
            grant_lantern(self.service, actor=HOLDER, holder_confirmed=False)

    def test_business_cannot_widen_scope(self) -> None:
        grant_lantern(self.service)
        with self.assertRaises(PermissionDenied):
            grant_lantern(
                self.service,
                actor=BUSINESS,
                channels=("stage", "promotion", "merchandise"),
                holder_confirmed=False,
            )
        # 缩窄范围不需要权利人再确认。
        narrower = grant_lantern(
            self.service,
            grant_id="G-0002",
            actor=BUSINESS,
            channels=("stage",),
            holder_confirmed=False,
        )
        self.assertEqual(["stage"], narrower["scope"]["channels"])

    def test_holder_can_widen_scope(self) -> None:
        grant_lantern(self.service)
        widened = grant_lantern(
            self.service,
            grant_id="G-0002",
            actor=HOLDER,
            channels=("stage", "promotion", "merchandise"),
            holder_confirmed=True,
        )
        self.assertIn("merchandise", widened["scope"]["channels"])

    def test_business_narrowing_does_not_shadow_holder_grant(self) -> None:
        grant_lantern(self.service)
        # 商务把范围缩到只剩 stage；其未确认的新授权不应遮蔽权利人已确认的原授权。
        grant_lantern(
            self.service,
            grant_id="G-0002",
            actor=BUSINESS,
            channels=("stage",),
            holder_confirmed=False,
        )
        decision = self.service.can_use("A-lantern", "P-troupe", "promotion")
        self.assertTrue(decision.allowed)
        self.assertEqual("G-0001", decision.grant_id)

    # ---------- 使用登记与渠道判定 ----------

    def test_can_use_reports_scope_term_and_withdrawal(self) -> None:
        grant_lantern(self.service)
        self.assertTrue(self.service.can_use("A-lantern", "P-troupe", "stage").allowed)
        denied = self.service.can_use("A-lantern", "P-troupe", "merchandise")
        self.assertFalse(denied.allowed)
        self.assertEqual("channel_not_in_scope", denied.reason)
        no_partner = self.service.can_use("A-lantern", "P-other", "stage")
        self.assertFalse(no_partner.allowed)
        self.service.withdraw_license(
            asset_id="A-lantern", effective_at=T + DAY, reason="传承人要求撤回", actor=HOLDER
        )
        self.assertTrue(
            self.service.can_use("A-lantern", "P-troupe", "stage", as_of=T).allowed
        )
        after = self.service.can_use("A-lantern", "P-troupe", "stage", as_of=T + 2 * DAY)
        self.assertFalse(after.allowed)
        self.assertEqual("withdrawn", after.reason)

    def test_use_without_valid_grant_is_rejected(self) -> None:
        grant_lantern(self.service)
        with self.assertRaises(LicenseNotUsable):
            self.service.record_use(
                asset_id="A-lantern",
                partner_id="P-troupe",
                channel="merchandise",
                use_time=T,
                actor=OPS,
            )

    def test_living_heritage_use_requires_note(self) -> None:
        grant_lantern(self.service)
        with self.assertRaises(HeritageNoteRequired):
            self.service.record_use(
                asset_id="A-lantern",
                partner_id="P-troupe",
                channel="stage",
                use_time=T,
                actor=OPS,
                night=NIGHT,
            )

    # ---------- 锁场与撤回 ----------

    def _record_night_use(self, version: str = "v1", use_time=None):
        return self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=use_time or T,
            actor=OPS,
            night=NIGHT,
            program_version=version,
            heritage_note="传承人确认的纹样说明",
        )

    def test_freeze_locks_night_but_preserves_completed_evidence(self) -> None:
        grant_lantern(self.service)
        use = self._record_night_use("v1")
        freeze = self.service.freeze_release(
            night=NIGHT, release_version="v1", actor=OPS
        )
        self.assertEqual([use["use_id"]], [item["use_id"] for item in freeze["snapshot"]])
        with self.assertRaises(ReleaseFrozen):
            self._record_night_use("v2")
        with self.assertRaises(ReleaseFrozen):
            self.service.freeze_release(night=NIGHT, release_version="v2", actor=OPS)
        basis = self.service.use_basis(use["use_id"])
        self.assertEqual("v1", basis["freeze"]["release_version"])
        self.assertEqual("v1", basis["use"]["program_version"])

    def test_withdrawal_only_affects_future_uses(self) -> None:
        grant_lantern(self.service)
        before = self._record_night_use(use_time=T - 2 * DAY)
        future = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="promotion",
            use_time=T + 2 * DAY,
            actor=OPS,
            night="2026-09-28",
            heritage_note="传承人确认的纹样说明",
        )
        self.service.withdraw_license(
            asset_id="A-lantern", effective_at=T, reason="纹样使用争议", actor=HOLDER
        )
        case = self.service.removal_cases[f"RC-{future['use_id']}"]
        self.assertEqual("open", case["status"])
        self.assertNotIn(f"RC-{before['use_id']}", self.service.removal_cases)
        obligation = self.service.obligations[f"OB-{before['use_id']}"]
        self.assertEqual("withdrawal_explanation", obligation["kind"])
        # 撤下后登记回执结案；已完成的展示保留证据且仍有说明义务。
        receipt = self.service.confirm_removal(
            receipt_no="R-001", case_id=case["case_id"], confirmed_by=OPS
        )
        self.assertEqual("confirmed", self.service.removal_cases[case["case_id"]]["status"])
        # 同一回执重复提交不重复计数。
        again = self.service.confirm_removal(
            receipt_no="R-001", case_id=case["case_id"], confirmed_by=OPS
        )
        self.assertEqual(receipt["event_id"], again["event_id"])
        self.assertEqual(1, sum(r["receipt_no"] == "R-001" for r in self.service.receipts.values()))
        self.assertIn(f"OB-{before['use_id']}", self.service.obligations)

    def test_receipt_same_number_different_scope_is_disputed(self) -> None:
        grant_lantern(self.service)
        future = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="promotion",
            use_time=T + 2 * DAY,
            actor=OPS,
            night="2026-09-28",
            heritage_note="传承人确认的纹样说明",
        )
        self.service.withdraw_license(
            asset_id="A-lantern", effective_at=T, reason="争议", actor=HOLDER
        )
        case_id = f"RC-{future['use_id']}"
        self.service.confirm_removal(
            receipt_no="R-002",
            case_id=case_id,
            confirmed_by=OPS,
            scope={"channels": ["promotion"]},
        )
        with self.assertRaises(ReceiptConflict):
            self.service.confirm_removal(
                receipt_no="R-002",
                case_id=case_id,
                confirmed_by=OPS,
                asset_id="A-other",
                scope={"channels": ["merchandise"]},
            )
        disputes = self.service.disputes()
        self.assertEqual("removal_receipt", disputes[-1]["kind"])
        # 争议提交未结案也未重复计数。
        self.assertEqual(1, len(self.service.receipts))

    def test_business_cannot_withdraw(self) -> None:
        grant_lantern(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_license(
                asset_id="A-lantern", effective_at=T, reason="x", actor=BUSINESS
            )

    # ---------- 展位并行方案 ----------

    def test_booth_confirmed_only_once_per_night(self) -> None:
        grant_lantern(self.service)
        first = self.service.confirm_booth(
            asset_id="A-lantern",
            partner_id="P-troupe",
            booth="B-07",
            night=NIGHT,
            plan_id="plan-A",
            use_time=T,
            actor=OPS,
        )
        # 同方案重试幂等。
        retry = self.service.confirm_booth(
            asset_id="A-lantern",
            partner_id="P-troupe",
            booth="B-07",
            night=NIGHT,
            plan_id="plan-A",
            use_time=T,
            actor=OPS,
        )
        self.assertEqual(first["use_id"], retry["use_id"])
        # 并行方案只能确认一次。
        with self.assertRaises(BoothAlreadyConfirmed):
            self.service.confirm_booth(
                asset_id="A-lantern",
                partner_id="P-other",
                booth="B-07",
                night=NIGHT,
                plan_id="plan-B",
                use_time=T,
                actor=OPS,
            )
        # 另一夜晚不受影响。
        other_night = self.service.confirm_booth(
            asset_id="A-lantern",
            partner_id="P-other",
            booth="B-07",
            night="2026-09-27",
            plan_id="plan-B",
            use_time=T + DAY,
            actor=OPS,
        )
        self.assertIsNotNone(other_night["use_id"])

    # ---------- 换节目 ----------

    def test_program_swap_creates_orphan_obligation_before_freeze(self) -> None:
        grant_lantern(self.service)
        old = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=T,
            actor=OPS,
            night=NIGHT,
            program_version="v1",
            planned=True,
            heritage_note="说明",
        )
        self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=T + timedelta(hours=1),
            actor=OPS,
            night=NIGHT,
            program_version="v2",
            planned=True,
            heritage_note="说明",
        )
        obligation = self.service.obligations[f"OBX-{old['use_id']}"]
        self.assertEqual("orphan_commitment", obligation["kind"])

    # ---------- 待办与重启 ----------

    def test_pending_matters_cover_removal_obligation_and_expiry(self) -> None:
        self.service.grant_license(
            asset_id="A-space",
            title="航天展品展板",
            origin="航天合作方供图",
            right_holder="holder-space",
            partner_id="P-troupe",
            channels=("exhibit",),
            valid_from=T - 30 * DAY,
            valid_to=T - DAY,
            actor=HOLDER,
        )
        grant_lantern(self.service)
        self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="promotion",
            use_time=T + 2 * DAY,
            actor=OPS,
            night="2026-09-28",
            heritage_note="传承人确认的纹样说明",
        )
        self.service.withdraw_license(
            asset_id="A-lantern", effective_at=T, reason="争议", actor=HOLDER
        )
        self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=T - 2 * DAY,
            actor=OPS,
            night="2026-09-24",
            heritage_note="说明",
        )
        kinds = {m.kind for m in self.service.pending_matters()}
        self.assertIn("removal_open", kinds)
        self.assertIn("explanation_obligation", kinds)
        self.assertIn("license_expired", kinds)

    def test_restart_rebuilds_state_and_pending_matters(self) -> None:
        grant_lantern(self.service)
        completed = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=T - 2 * DAY,
            actor=OPS,
            night="2026-09-24",
            heritage_note="说明",
        )
        use = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="promotion",
            use_time=T + 2 * DAY,
            actor=OPS,
            night="2026-09-28",
            heritage_note="说明",
        )
        self.service.withdraw_license(
            asset_id="A-lantern", effective_at=T, reason="争议", actor=HOLDER
        )
        case_id = f"RC-{use['use_id']}"
        self.service.confirm_removal(
            receipt_no="R-010", case_id=case_id, confirmed_by=OPS
        )
        directory = self.service.store._path.parent  # noqa: SLF001
        reopened = LicensingService.open(directory)
        self.assertIn("A-lantern", reopened.assets)
        self.assertEqual("confirmed", reopened.removal_cases[case_id]["status"])
        self.assertIn("R-010", reopened.receipts)
        # 已完成的撤展不再出现在未完成事项；说明义务仍在。
        pending = reopened.pending_matters()
        self.assertNotIn("removal_open", {m.kind for m in pending if m.ref_id == case_id})
        self.assertTrue(any(m.kind == "explanation_obligation" for m in pending))
        decision = reopened.can_use("A-lantern", "P-troupe", "stage", as_of=T + 5 * DAY)
        self.assertFalse(decision.allowed)

    def test_use_basis_traces_grant_holder_and_responsible_party(self) -> None:
        grant_lantern(self.service)
        use = self.service.record_use(
            asset_id="A-lantern",
            partner_id="P-troupe",
            channel="stage",
            use_time=T,
            actor=OPS,
            night=NIGHT,
            program_version="v1",
            heritage_note="传承人确认的纹样说明",
        )
        basis = self.service.use_basis(use["use_id"])
        self.assertEqual("holder-zigong", basis["grant"]["right_holder"])
        self.assertEqual("ops-stage", basis["use"]["recorded_by"]["actor_id"])
        self.assertEqual("A-lantern", basis["asset"]["asset_id"])
        self.assertTrue(basis["grant"]["holder_confirmed"])

    # ---------- 事件幂等与争议 ----------

    def test_same_event_id_is_idempotent(self) -> None:
        grant_lantern(self.service, event_id="EVT-1", grant_id="G-1001")
        again = grant_lantern(self.service, event_id="EVT-1", grant_id="G-1001")
        self.assertEqual("G-1001", again["grant_id"])
        self.assertEqual(1, sum(1 for _ in self.service.store.events()))

    def test_same_event_id_different_scope_is_disputed(self) -> None:
        grant_lantern(self.service, event_id="EVT-2", grant_id="G-1002")
        with self.assertRaises(EventConflict):
            grant_lantern(
                self.service,
                event_id="EVT-2",
                grant_id="G-1002",
                channels=("stage", "promotion", "merchandise"),
            )
        self.assertEqual(1, sum(1 for _ in self.service.store.events()))
        self.assertEqual("event_id", self.service.disputes()[-1]["kind"])

    def test_disputes_survive_restart(self) -> None:
        grant_lantern(self.service, event_id="EVT-3", grant_id="G-1003")
        with self.assertRaises(EventConflict):
            grant_lantern(
                self.service,
                event_id="EVT-3",
                grant_id="G-1003",
                channels=("merchandise",),
            )
        directory = self.service.store._path.parent  # noqa: SLF001
        reopened = LicensingService.open(directory)
        self.assertTrue(reopened.disputes())


if __name__ == "__main__":
    unittest.main()
