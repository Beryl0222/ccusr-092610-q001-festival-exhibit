import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from festival_exhibit.service import LicenseService, ServiceError

HKT = "+08:00"
GRANT_AT = f"2026-09-01T09:00:00{HKT}"
VALID_FROM = f"2026-09-01T00:00:00{HKT}"
VALID_TO = f"2026-10-31T23:59:59{HKT}"


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / "journal.jsonl"
        self.service = LicenseService(self.journal)

    def tearDown(self) -> None:
        self.assertFalse(self.journal.exists() and not self.journal.read_text(encoding="utf-8").strip())

    def grant_pattern_license(self, **overrides):
        params = dict(
            event_id="evt-grant-001",
            occurred_at=GRANT_AT,
            license_id="lic-zigong-lantern",
            asset_id="asset-traditional-pattern",
            source="自贡灯彩扎制技艺纹样库",
            right_holder="holder-lantern-master",
            channels=["stage", "promotion"],
            valid_from=VALID_FROM,
            valid_to=VALID_TO,
            actor="holder-lantern-master",
            partners=["partner-stage", "partner-promo"],
        )
        params.update(overrides)
        return self.service.grant_license(**params)

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(ServiceError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(code, ctx.exception.code)


class GrantAndHeritageTests(ServiceTestCase):
    def test_grant_and_can_use(self):
        self.grant_pattern_license()
        verdict = self.service.can_use(
            partner_id="partner-stage",
            asset_id="asset-traditional-pattern",
            channel="stage",
            at=f"2026-09-25T20:00:00{HKT}",
        )
        self.assertTrue(verdict["allowed"])
        self.assertEqual("lic-zigong-lantern", verdict["basis"]["license_id"])
        self.assertEqual("holder-lantern-master", verdict["basis"]["right_holder"])

    def test_can_use_rejects_out_of_scope_channel_and_stranger(self):
        self.grant_pattern_license()
        verdict = self.service.can_use(
            partner_id="partner-stage",
            asset_id="asset-traditional-pattern",
            channel="merchandise",
            at=f"2026-09-25T20:00:00{HKT}",
        )
        self.assertFalse(verdict["allowed"])
        self.assertIn("channel_out_of_scope", verdict["reasons"])
        stranger = self.service.can_use(
            partner_id="partner-merch",
            asset_id="asset-traditional-pattern",
            channel="stage",
            at=f"2026-09-25T20:00:00{HKT}",
        )
        self.assertFalse(stranger["allowed"])
        self.assertIsNone(stranger["basis"])

    def test_heritage_note_requires_holder_confirmation(self):
        self.assert_error(
            "heritage_confirmation_required",
            self.grant_pattern_license,
            event_id="evt-grant-heritage",
            actor="staff-business-01",
            heritage_note="纹样承载自贡灯会活态传承叙事",
            heritage_confirmed_by="staff-business-01",
        )
        result = self.grant_pattern_license(
            event_id="evt-grant-heritage-ok",
            heritage_note="纹样承载自贡灯会活态传承叙事",
            heritage_confirmed_by="holder-lantern-master",
        )
        self.assertFalse(result["idempotent"])

    def test_business_staff_cannot_expand_scope(self):
        self.grant_pattern_license()
        self.assert_error(
            "scope_expansion_forbidden",
            self.grant_pattern_license,
            event_id="evt-amend-expand",
            occurred_at=f"2026-09-10T09:00:00{HKT}",
            actor="staff-business-01",
            channels=["stage", "promotion", "merchandise"],
        )
        self.assert_error(
            "scope_expansion_forbidden",
            self.grant_pattern_license,
            event_id="evt-amend-term",
            occurred_at=f"2026-09-10T09:00:00{HKT}",
            actor="staff-business-01",
            valid_to=f"2026-12-31T23:59:59{HKT}",
        )
        # 收窄许可允许商务人员办理
        narrowed = self.grant_pattern_license(
            event_id="evt-amend-narrow",
            occurred_at=f"2026-09-10T09:00:00{HKT}",
            actor="staff-business-01",
            channels=["stage"],
        )
        self.assertEqual(2, narrowed["event"]["version"])
        # 权利人本人可以扩大许可
        expanded = self.grant_pattern_license(
            event_id="evt-amend-holder",
            occurred_at=f"2026-09-11T09:00:00{HKT}",
            channels=["stage", "promotion", "merchandise"],
        )
        self.assertEqual(3, expanded["event"]["version"])
        verdict = self.service.can_use(
            partner_id="partner-stage",
            asset_id="asset-traditional-pattern",
            channel="merchandise",
            at=f"2026-09-25T20:00:00{HKT}",
        )
        self.assertTrue(verdict["allowed"])

    def test_idempotent_retry_and_conflicting_event_id(self):
        first = self.grant_pattern_license()
        retry = self.grant_pattern_license()
        self.assertTrue(retry["idempotent"])
        self.assertEqual(first["event"]["event_id"], retry["event"]["event_id"])
        self.assert_error(
            "event_conflict",
            self.grant_pattern_license,
            source="另一来源",
        )
        lines = self.journal.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))


class UseAndFreezeTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.grant_pattern_license()

    def record_use(self, **overrides):
        params = dict(
            event_id="evt-use-001",
            occurred_at=f"2026-09-25T21:00:00{HKT}",
            use_id="use-001",
            partner_id="partner-stage",
            license_id="lic-zigong-lantern",
            channel="stage",
            use_time=f"2026-09-25T20:00:00{HKT}",
            actor="staff-pm-01",
            release_id="release-gala",
            release_version=3,
            position="booth-A1",
            promotion_refs=["poster-2026-09"],
        )
        params.update(overrides)
        return self.service.record_use(**params)

    def test_record_use_and_explain(self):
        self.record_use()
        explained = self.service.explain_use("use-001")
        self.assertEqual("staff-pm-01", explained["responsible"])
        self.assertEqual("recorded", explained["status"])
        self.assertEqual("lic-zigong-lantern", explained["basis"]["license_id"])
        self.assertEqual(1, explained["basis"]["license_version"])
        self.assertEqual(["promotion", "stage"], explained["basis"]["scope_at_use"]["channels"])
        self.assertEqual("自贡灯彩扎制技艺纹样库", explained["basis"]["source"])
        self.assertIsNone(explained["withdrawal"])

    def test_record_use_validation(self):
        self.assert_error("channel_out_of_scope", self.record_use, channel="merchandise")
        self.assert_error("partner_not_licensed", self.record_use, partner_id="partner-merch")
        self.assert_error("outside_license_term", self.record_use, use_time=f"2026-11-01T00:00:00{HKT}")
        self.assert_error("license_not_found", self.record_use, license_id="lic-missing")
        self.assert_error("invalid_time", self.record_use, use_time="2026-09-25T20:00:00")

    def test_freeze_locks_versions_and_booth_confirmed_once(self):
        frozen = self.service.freeze_event(
            event_id="evt-freeze-001",
            occurred_at=f"2026-09-25T18:00:00{HKT}",
            release_id="release-gala",
            event_day="2026-09-25",
            release_version=3,
            frozen_at=f"2026-09-25T18:00:00{HKT}",
            actor="staff-pm-01",
            positions=["booth-A1"],
            items=[{"program": "学生粤语朗诵", "version": 3}, {"program": "无人机灯光秀", "version": 2}],
        )
        self.assertEqual("EVENT_FROZEN", frozen["event"]["event_type"])
        self.assert_error(
            "position_conflict",
            self.service.freeze_event,
            event_id="evt-freeze-002",
            occurred_at=f"2026-09-25T18:05:00{HKT}",
            release_id="release-gala-parallel",
            event_day="2026-09-25",
            release_version=1,
            frozen_at=f"2026-09-25T18:05:00{HKT}",
            actor="staff-pm-02",
            positions=["booth-A1"],
        )
        # 同一展位换一天、或同一天换展位均可确认
        self.service.freeze_event(
            event_id="evt-freeze-003",
            occurred_at=f"2026-09-26T18:00:00{HKT}",
            release_id="release-gala-day2",
            event_day="2026-09-26",
            release_version=1,
            frozen_at=f"2026-09-26T18:00:00{HKT}",
            actor="staff-pm-01",
            positions=["booth-A1"],
        )
        self.service.freeze_event(
            event_id="evt-freeze-004",
            occurred_at=f"2026-09-25T18:10:00{HKT}",
            release_id="release-gala-booth-b",
            event_day="2026-09-25",
            release_version=1,
            frozen_at=f"2026-09-25T18:10:00{HKT}",
            actor="staff-pm-01",
            positions=["booth-B2"],
        )
        self.assert_error(
            "release_frozen",
            self.service.freeze_event,
            event_id="evt-freeze-005",
            occurred_at=f"2026-09-25T19:00:00{HKT}",
            release_id="release-gala",
            event_day="2026-09-25",
            release_version=4,
            frozen_at=f"2026-09-25T19:00:00{HKT}",
            actor="staff-pm-01",
        )


class WithdrawalAndReceiptTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.grant_pattern_license()
        # 撤回生效前已完成的展示
        self.service.record_use(
            event_id="evt-use-past",
            occurred_at=f"2026-09-20T21:00:00{HKT}",
            use_id="use-past",
            partner_id="partner-promo",
            license_id="lic-zigong-lantern",
            channel="promotion",
            use_time=f"2026-09-20T20:00:00{HKT}",
            actor="staff-pm-01",
            promotion_refs=["metro-lightbox-09"],
        )
        # 撤回生效后才会发生的使用
        self.service.record_use(
            event_id="evt-use-future",
            occurred_at=f"2026-09-24T10:00:00{HKT}",
            use_id="use-future",
            partner_id="partner-stage",
            license_id="lic-zigong-lantern",
            channel="stage",
            use_time=f"2026-09-27T20:00:00{HKT}",
            actor="staff-pm-02",
        )
        self.service.withdraw_license(
            event_id="evt-withdraw-001",
            occurred_at=f"2026-09-26T09:00:00{HKT}",
            license_id="lic-zigong-lantern",
            effective_at=f"2026-09-26T12:00:00{HKT}",
            reason="权利人与衍生品方另有独家安排",
            actor="holder-lantern-master",
        )

    def test_withdrawal_only_affects_future_uses(self):
        past = self.service.explain_use("use-past")
        self.assertEqual("completed", past["status"])
        self.assertIsNotNone(past["removal_case"])
        self.assertEqual("explanation", past["removal_case"]["kind"])
        future = self.service.explain_use("use-future")
        self.assertEqual("cancelled", future["status"])
        self.assertIsNone(future["removal_case"])
        # 生效时点之后不再允许登记新使用
        self.assert_error(
            "license_withdrawn",
            self.service.record_use,
            event_id="evt-use-late",
            occurred_at=f"2026-09-26T13:00:00{HKT}",
            use_id="use-late",
            partner_id="partner-stage",
            license_id="lic-zigong-lantern",
            channel="stage",
            use_time=f"2026-09-26T20:00:00{HKT}",
            actor="staff-pm-01",
        )
        # can_use 以撤回生效时点为界
        before = self.service.can_use(
            partner_id="partner-stage",
            asset_id="asset-traditional-pattern",
            channel="stage",
            at=f"2026-09-26T10:00:00{HKT}",
        )
        self.assertTrue(before["allowed"])
        after = self.service.can_use(
            partner_id="partner-stage",
            asset_id="asset-traditional-pattern",
            channel="stage",
            at=f"2026-09-26T13:00:00{HKT}",
        )
        self.assertFalse(after["allowed"])
        self.assertIn("license_withdrawn", after["reasons"])
        self.assert_error(
            "already_withdrawn",
            self.service.withdraw_license,
            event_id="evt-withdraw-002",
            occurred_at=f"2026-09-27T09:00:00{HKT}",
            license_id="lic-zigong-lantern",
            effective_at=f"2026-09-28T12:00:00{HKT}",
            reason="重复撤回",
            actor="holder-lantern-master",
        )

    def test_pending_removal_and_receipt_lifecycle(self):
        pending = self.service.pending_removals()
        self.assertEqual(1, len(pending))
        self.assertEqual("partner-promo", pending[0]["partner_id"])
        self.assertEqual(["use-past"], pending[0]["use_ids"])
        # 提交回执结案
        first = self.service.submit_removal_receipt(
            event_id="evt-receipt-001",
            occurred_at=f"2026-09-28T10:00:00{HKT}",
            partner_id="partner-promo",
            receipt_no="R-2026-001",
            asset_id="asset-traditional-pattern",
            scope=["promotion"],
            actor="partner-promo",
        )
        self.assertEqual("counted", first["outcome"])
        self.assertEqual([], self.service.pending_removals())
        # 相同回执重复提交不重复计数
        again = self.service.submit_removal_receipt(
            event_id="evt-receipt-002",
            occurred_at=f"2026-09-28T11:00:00{HKT}",
            partner_id="partner-promo",
            receipt_no="R-2026-001",
            asset_id="asset-traditional-pattern",
            scope=["promotion"],
            actor="partner-promo",
        )
        self.assertEqual("duplicate", again["outcome"])
        self.assertEqual([], self.service.disputes())
        # 编号相同但素材不同，进入争议且不计数
        conflict = self.service.submit_removal_receipt(
            event_id="evt-receipt-003",
            occurred_at=f"2026-09-28T12:00:00{HKT}",
            partner_id="partner-promo",
            receipt_no="R-2026-001",
            asset_id="asset-other-pattern",
            scope=["promotion"],
            actor="partner-promo",
        )
        self.assertEqual("disputed", conflict["outcome"])
        disputes = self.service.disputes()
        self.assertEqual(1, len(disputes))
        self.assertEqual("evt-receipt-001", disputes[0]["first_event_id"])
        self.assertEqual("evt-receipt-003", disputes[0]["conflicting_event_id"])

    def test_restart_recovers_pending_and_expired(self):
        report_before = self.service.recovery_report(f"2026-11-01T00:00:00{HKT}")
        self.assertEqual(1, len(report_before["pending_removals"]))
        self.assertEqual(1, len(report_before["expired_licenses"]))
        expired = report_before["expired_licenses"][0]
        self.assertTrue(expired["expired"])
        self.assertIsNotNone(expired["withdrawn"])
        self.assertEqual(["lic-zigong-lantern:partner-promo:explanation"], expired["open_cases"])
        # 服务重启：重放日志后视图一致
        reopened = LicenseService(self.journal)
        report_after = reopened.recovery_report(f"2026-11-01T00:00:00{HKT}")
        self.assertEqual(report_before, report_after)
        explained = reopened.explain_use("use-past")
        self.assertEqual("completed", explained["status"])
        self.assertEqual("staff-pm-01", explained["responsible"])


if __name__ == "__main__":
    unittest.main()
