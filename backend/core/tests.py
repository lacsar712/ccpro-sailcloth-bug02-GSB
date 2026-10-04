"""浸渍写入规则测试：非正树脂拦截、连点去重、交叉并发只留一版。"""

import threading
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from core.models import ClothRoll, DipRun, Loft

User = get_user_model()

def payload(roll_id, resin_pct, *, notes="", hours=None):
    return {
        "rollId": roll_id,
        "startedAt": timezone.now().isoformat(),
        "resinPct": resin_pct,
        "cureHours": hours,
        "notes": notes,
    }


class DipWriteTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.worker_a = User.objects.create_user(username="gumar", password="x")
        self.worker_b = User.objects.create_user(username="gumrb", password="x")
        self.loft = Loft.objects.create(name="测试帆布间")
        self.roll = ClothRoll.objects.create(
            loft=self.loft, roll_code="R-T1", fabric_weight_gsm=400
        )

    # ---- 非法树脂：中文挡住且不入库 ----

    def test_zero_and_negative_resin_rejected_in_chinese(self):
        self.client.force_authenticate(self.worker_a)
        for bad in ("0", "-1", "-0.01"):
            resp = self.client.post(
                "/api/dips/", payload(self.roll.id, bad), format="json"
            )
            self.assertEqual(resp.status_code, 400, bad)
            msg = str(resp.data)
            self.assertIn("树脂", msg, bad)
            self.assertIn("正", msg, bad)
        self.assertEqual(DipRun.objects.filter(roll=self.roll).count(), 0)

    def test_missing_resin_rejected_and_not_saved(self):
        self.client.force_authenticate(self.worker_a)
        data = payload(self.roll.id, "28")
        del data["resinPct"]
        resp = self.client.post("/api/dips/", data, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DipRun.objects.filter(roll=self.roll).count(), 0)

    # ---- 同一卷连点：最后只留一版合法记录 ----

    def test_rapid_clicks_keep_only_last_version(self):
        self.client.force_authenticate(self.worker_a)
        t2 = timezone.now()

        r1 = self.client.post(
            "/api/dips/",
            payload(self.roll.id, "28.50", notes="第一次"),
            format="json",
        )
        self.assertEqual(r1.status_code, 201, r1.data)
        r2 = self.client.post(
            "/api/dips/",
            {
                "rollId": self.roll.id,
                "startedAt": t2.isoformat(),
                "resinPct": "29.75",
                "cureHours": None,
                "notes": "连点第二次",
            },
            format="json",
        )
        self.assertEqual(r2.status_code, 201, r2.data)

        runs = DipRun.objects.filter(roll=self.roll)
        self.assertEqual(runs.count(), 1)
        only = runs.get()
        self.assertEqual(only.resin_pct, Decimal("29.75"))
        self.assertEqual(only.notes, "连点第二次")
        self.assertEqual(only.started_at, t2)

    # ---- 两名浸胶工交叉给同一卷写：只许一版合法记录 ----

    def test_two_workers_concurrent_write_keeps_one(self):
        client_a = self._authed_client(self.worker_a)
        client_b = self._authed_client(self.worker_b)
        barrier = threading.Barrier(2)
        errors = []

        def write(client, resin):
            barrier.wait()
            try:
                resp = client.post(
                    "/api/dips/", payload(self.roll.id, resin), format="json"
                )
                if resp.status_code != 201:
                    errors.append((resin, resp.status_code, resp.data))
            except Exception as exc:  # noqa: BLE001
                errors.append((resin, repr(exc)))
            finally:
                connection.close()

        t_a = threading.Thread(target=write, args=(client_a, "28.50"))
        t_b = threading.Thread(target=write, args=(client_b, "29.50"))
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)

        self.assertFalse(errors, errors)
        runs = DipRun.objects.filter(roll=self.roll)
        self.assertEqual(runs.count(), 1)
        self.assertIn(runs.get().resin_pct, (Decimal("28.50"), Decimal("29.50")))

    def test_illegal_submission_does_not_overwrite_existing(self):
        self.client.force_authenticate(self.worker_a)
        ok = self.client.post(
            "/api/dips/", payload(self.roll.id, "28.50", notes="合法"), format="json"
        )
        self.assertEqual(ok.status_code, 201)
        bad = self.client.post(
            "/api/dips/", payload(self.roll.id, "-5", notes="非法"), format="json"
        )
        self.assertEqual(bad.status_code, 400)
        only = DipRun.objects.get(roll=self.roll)
        self.assertEqual(only.resin_pct, Decimal("28.50"))
        self.assertEqual(only.notes, "合法")

    def test_different_rolls_keep_separate_records(self):
        other = ClothRoll.objects.create(
            loft=self.loft, roll_code="R-T9", fabric_weight_gsm=400
        )
        self.client.force_authenticate(self.worker_a)
        r1 = self.client.post(
            "/api/dips/", payload(self.roll.id, "28.00"), format="json"
        )
        r2 = self.client.post("/api/dips/", payload(other.id, "30.00"), format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(DipRun.objects.count(), 2)

    def _authed_client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client


class DipRunMigrationTests(TransactionTestCase):
    """0002 迁移：清理非正树脂与同卷重复，再加约束。"""

    def migrate_to(self, target):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate([("core", target)])
        return executor.loader.project_state([("core", target)]).apps

    def test_migration_cleans_dirty_and_duplicates_then_constrains(self):
        # 回退到 0001（无 check / unique 约束），造脏数据与连点重复
        old_apps = self.migrate_to("0001_initial")
        OldLoft = old_apps.get_model("core", "Loft")
        OldRoll = old_apps.get_model("core", "ClothRoll")
        OldDipRun = old_apps.get_model("core", "DipRun")

        loft = OldLoft.objects.create(name="迁移测试帆布间")
        r1 = OldRoll.objects.create(
            loft_id=loft.id, roll_code="R-M1", fabric_weight_gsm=400
        )
        r2 = OldRoll.objects.create(
            loft_id=loft.id, roll_code="R-M2", fabric_weight_gsm=400
        )
        now = timezone.now()

        def make(roll_id, minutes_ago, resin, note):
            return OldDipRun.objects.create(
                roll_id=roll_id,
                started_at=now - timedelta(minutes=minutes_ago),
                resin_pct=Decimal(resin),
                notes=note,
            )

        keep = make(r1.id, 5, "28.00", "最新合法")
        make(r1.id, 10, "0.00", "零值脏数据")
        make(r1.id, 20, "-3.00", "负数脏数据")
        make(r1.id, 30, "27.00", "更早合法")
        make(r2.id, 5, "30.00", "另一卷")

        # 前进到最新：执行清理 + 加约束
        self.migrate_to("0002_diprun_one_per_roll")

        self.assertEqual(
            list(DipRun.objects.filter(roll_id=r1.id).values_list("id", flat=True)),
            [keep.id],
        )
        self.assertEqual(DipRun.objects.filter(roll_id=r2.id).count(), 1)
        self.assertFalse(DipRun.objects.filter(resin_pct__lte=0).exists())

        # 非正树脂在数据库层同样被拒绝
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DipRun.objects.create(
                    roll_id=r2.id,
                    started_at=now,
                    resin_pct=Decimal("0"),
                )

        # 同卷第二条在数据库层被唯一约束拒绝
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DipRun.objects.create(
                    roll_id=r2.id,
                    started_at=now,
                    resin_pct=Decimal("31.00"),
                )
        self.assertEqual(DipRun.objects.filter(roll_id=r2.id).count(), 1)
