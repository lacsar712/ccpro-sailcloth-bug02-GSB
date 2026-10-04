"""浸渍写入：非法树脂拦截 + 同卷连点/交叉提交只留一条合法记录。

并发用例需文件型数据库（内存 SQLite 不跨线程），见 config/settings_test.py；
生产 Postgres 下 SELECT ... FOR UPDATE 同样保证串行。
"""

import threading
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from .models import ClothRoll, DipRun, Loft


def _payload(roll, resin_pct, started_at=None):
    return {
        "rollId": roll.id,
        "startedAt": (started_at or timezone.now()).isoformat(),
        "resinPct": str(resin_pct),
        "cureHours": None,
        "notes": "",
    }


class ResinValidationTests(APITestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="w1", password="x")
        self.client.force_authenticate(self.user)
        loft = Loft.objects.create(name="北岸帆布间")
        self.roll = ClothRoll.objects.create(
            loft=loft, roll_code="R-01", fabric_weight_gsm=380
        )

    def test_zero_resin_is_rejected_in_chinese_and_not_saved(self):
        resp = self.client.post("/api/dips/", _payload(self.roll, "0"), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resinPct", resp.data)
        self.assertIn("大于 0", resp.data["resinPct"][0])
        self.assertEqual(DipRun.objects.count(), 0)

    def test_negative_resin_is_rejected_in_chinese_and_not_saved(self):
        resp = self.client.post("/api/dips/", _payload(self.roll, "-5.5"), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resinPct", resp.data)
        self.assertIn("大于 0", resp.data["resinPct"][0])
        self.assertEqual(DipRun.objects.count(), 0)

    def test_non_numeric_resin_is_rejected(self):
        payload = _payload(self.roll, "abc")
        resp = self.client.post("/api/dips/", payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resinPct", resp.data)
        self.assertEqual(DipRun.objects.count(), 0)

    def test_small_positive_resin_is_accepted(self):
        resp = self.client.post("/api/dips/", _payload(self.roll, "0.01"), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(DipRun.objects.count(), 1)
        self.assertEqual(DipRun.objects.get().resin_pct, Decimal("0.01"))


class DuplicateSubmitTests(APITestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="w1", password="x")
        self.client.force_authenticate(self.user)
        loft = Loft.objects.create(name="北岸帆布间")
        self.roll = ClothRoll.objects.create(
            loft=loft, roll_code="R-02", fabric_weight_gsm=380
        )

    def test_double_click_same_roll_leaves_single_latest_record(self):
        now = timezone.now()
        r1 = self.client.post(
            "/api/dips/", _payload(self.roll, "28.00", now - timedelta(minutes=2)),
            format="json",
        )
        r2 = self.client.post(
            "/api/dips/", _payload(self.roll, "31.50", now - timedelta(minutes=1)),
            format="json",
        )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        records = DipRun.objects.filter(roll=self.roll)
        self.assertEqual(records.count(), 1, "同卷连点后只能留下一条浸渍记录")
        self.assertEqual(records.get().resin_pct, Decimal("31.50"))


class CrossWorkerConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.workers = [
            User.objects.create_user(username="gongren_a", password="x"),
            User.objects.create_user(username="gongren_b", password="x"),
        ]
        loft = Loft.objects.create(name="交叉卷帆布间")
        self.roll = ClothRoll.objects.create(
            loft=loft, roll_code="R-X", fabric_weight_gsm=400
        )

    def test_two_workers_racing_same_roll_leave_one_valid_record(self):
        values = [Decimal("22.00"), Decimal("27.50"), Decimal("30.00"),
                  Decimal("24.25"), Decimal("29.99"), Decimal("26.00")]
        barrier = threading.Barrier(len(values))
        results = []
        results_lock = threading.Lock()

        def post(worker, resin):
            client = APIClient()
            client.force_authenticate(worker)
            barrier.wait(timeout=10)
            try:
                resp = client.post(
                    "/api/dips/", _payload(self.roll, str(resin)), format="json"
                )
                with results_lock:
                    results.append(
                        (resp.status_code, resp.data if resp.status_code >= 400 else None)
                    )
            except Exception as exc:  # noqa: BLE001 - 线程异常必须落到主线程断言
                with results_lock:
                    results.append(("EXC", repr(exc)))

        threads = [
            threading.Thread(target=post, args=(self.workers[i % 2], v))
            for i, v in enumerate(values)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "并发请求超时未结束")

        failures = [r for r in results if r[0] != 201]
        self.assertEqual(failures, [], f"并发写入出现失败响应：{failures}")

        records = list(DipRun.objects.filter(roll=self.roll))
        self.assertEqual(len(records), 1, "两名浸胶工交叉写入后同卷只能有一条记录")
        only = records[0]
        self.assertIn(only.resin_pct, values, "留下的必须是某次合法提交的值")
        self.assertGreater(only.resin_pct, Decimal("0"))
