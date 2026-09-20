"""用工计酬后端的端到端契约测试。

通过真实 HTTP 服务（内存 SQLite）验证业务规则，而非直接调用内部方法。
"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from labor.app import Service
from labor.httpapi import create_handler


class Client:
    def __init__(self, base_url):
        self.base_url = base_url

    def call(self, method, path, token=None, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        # 每个用例独立内存库，避免封账、统计等跨用例串数据。
        self.service = Service(":memory:")
        self.tokens = self.service.registry.bootstrap_staff_tokens()
        handler = create_handler(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = Client(f"http://127.0.0.1:{self.server.server_port}")
        self.tag = f"t{self._testMethodName}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()

    # --- 辅助 ---
    def staff(self, role):
        return self.tokens[role]

    def make_worker(self, name):
        status, body = self.api.call("POST", "/api/workers", self.staff("hr"),
                                     {"name": name})
        self.assertEqual(status, 200, body)
        return body["id"], body["token"]

    def make_operation(self, code, hourly="20.00", piece="2.00"):
        status, body = self.api.call("POST", "/api/operations", self.staff("production"),
                                     {"code": code, "name": f"工序{code}",
                                      "hourly_rate_yuan": hourly,
                                      "piece_rate_yuan": piece})
        self.assertEqual(status, 200, body)
        return body["id"]

    def qualify(self, worker_id, op_id, valid_until="2026-12-31", trained_at="2026-09-01"):
        s, _ = self.api.call("POST", f"/api/workers/{worker_id}/trainings",
                             self.staff("hr"),
                             {"trained_at": trained_at, "valid_until": valid_until})
        self.assertEqual(s, 200)
        s, _ = self.api.call(
            "POST", f"/api/workers/{worker_id}/health-certificates",
            self.staff("hr"),
            {"certificate_no": f"H{worker_id}", "issued_at": "2026-09-01",
             "valid_until": valid_until})
        self.assertEqual(s, 200)
        s, _ = self.api.call(
            "POST", f"/api/workers/{worker_id}/operations/{op_id}/grant",
            self.staff("hr"), {})
        self.assertEqual(s, 200)

    # --- 1. 资质闸门 ---
    def test_assign_blocked_without_qualifications(self):
        wid, _ = self.make_worker(f"阿珍-{self.tag}")
        op_id = self.make_operation(f"P-{self.tag}")
        s, body = self.api.call("POST", "/api/shifts", self.staff("leader"),
                                {"operation_id": op_id,
                                 "planned_start": "2026-09-20T08:00:00",
                                 "planned_end": "2026-09-20T12:00:00"})
        self.assertEqual(s, 200, body)
        shift_id = body["id"]
        s, body = self.api.call(
            "POST", f"/api/shifts/{shift_id}/assignments", self.staff("leader"),
            {"worker_id": wid})
        self.assertEqual(s, 422)
        self.assertEqual(body["error"], "eligibility_failed")
        joined = "；".join(body["details"]["reasons"])
        self.assertIn("食品安全培训", joined)
        self.assertIn("健康证明", joined)
        self.assertIn("操作授权", joined)

        self.qualify(wid, op_id)
        s, body = self.api.call(
            "POST", f"/api/shifts/{shift_id}/assignments", self.staff("leader"),
            {"worker_id": wid})
        self.assertEqual(s, 200, body)
        self.assertTrue(body["eligibility_snapshot"]["passed"])

    def test_expired_health_certificate_blocks_assignment(self):
        wid, _ = self.make_worker(f"阿强-{self.tag}")
        op_id = self.make_operation(f"Q-{self.tag}")
        self.qualify(wid, op_id, valid_until="2026-09-10")  # 已过期
        s, body = self.api.call("POST", "/api/shifts", self.staff("leader"),
                                {"operation_id": op_id,
                                 "planned_start": "2026-09-20T08:00:00",
                                 "planned_end": "2026-09-20T12:00:00"})
        shift_id = body["id"]
        s, body = self.api.call(
            "POST", f"/api/shifts/{shift_id}/assignments", self.staff("leader"),
            {"worker_id": wid})
        self.assertEqual(s, 422)
        self.assertTrue(any("健康证明" in r for r in body["details"]["reasons"]))

    # --- 2. 跨午夜 + 休息中断拆分 ---
    def test_cross_midnight_and_breaks_split(self):
        wid, _ = self.make_worker(f"阿夜-{self.tag}")
        op_id = self.make_operation(f"N-{self.tag}", hourly="24.00")
        self.qualify(wid, op_id)
        s, body = self.api.call("POST", "/api/shifts", self.staff("leader"),
                                {"operation_id": op_id,
                                 "planned_start": "2026-09-20T22:00:00",
                                 "planned_end": "2026-09-21T03:00:00"})
        shift_id = body["id"]
        self.api.call("POST", f"/api/shifts/{shift_id}/assignments",
                      self.staff("leader"), {"worker_id": wid})

        # 两条原始打卡（0.5 小时夜宵休息，跨午夜）。
        s, p1 = self.api.call("POST", "/api/punches", self.staff("leader"),
                              {"worker_id": wid, "scan_code": "GATE1", "event_type": "in",
                               "event_time": "2026-09-20T22:00:00",
                               "client_event_id": f"{self.tag}-in"})
        s, p2 = self.api.call("POST", "/api/punches", self.staff("leader"),
                              {"worker_id": wid, "scan_code": "GATE1", "event_type": "out",
                               "event_time": "2026-09-21T03:00:00",
                               "client_event_id": f"{self.tag}-out"})

        s, body = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                                {"confirm_key": f"R-{self.tag}-1", "worker_id": wid,
                                 "operation_id": op_id, "shift_id": shift_id,
                                 "started_at": "2026-09-20T22:00:00",
                                 "ended_at": "2026-09-21T03:00:00",
                                 "start_punch_event_id": p1["id"],
                                 "end_punch_event_id": p2["id"],
                                 "breaks": [{"start": "2026-09-20T23:30:00",
                                             "end": "2026-09-21T00:00:00"}],
                                 "category": "regular"})
        self.assertEqual(s, 200, body)
        # 5 小时 - 0.5 小时休息 = 270 分钟，按日历日两段。
        self.assertEqual(body["total_minutes"], 270)
        dates = sorted(seg["segment_date"] for seg in body["segments"])
        self.assertEqual(dates, ["2026-09-20", "2026-09-21"])
        day_minutes = {seg["segment_date"]: seg["minutes"] for seg in body["segments"]}
        self.assertEqual(day_minutes["2026-09-20"], 90)   # 22:00-23:30
        self.assertEqual(day_minutes["2026-09-21"], 180)  # 00:00-03:00
        # 24 元/小时 → 36 元 + 72 元
        amounts = sorted(item["amount_cents"] for item in body["pay_items"])
        self.assertEqual(amounts, [3600, 7200])

    # --- 3. 培训等待与设备停机保底单独分类 ---
    def test_training_and_standby_categories(self):
        wid, _ = self.make_worker(f"阿等-{self.tag}")
        op_id = self.make_operation(f"S-{self.tag}", hourly="20.00")
        self.qualify(wid, op_id)
        # 培训补贴 15 元/小时、停机保底 12 元/小时。
        self.assertEqual(self.api.call("PUT", "/api/settings/training_allowance_cents_per_hour",
                                       self.staff("admin"), {"value_cents": 1500})[0], 200)
        self.assertEqual(self.api.call("PUT", "/api/settings/standby_guarantee_cents_per_hour",
                                       self.staff("admin"), {"value_cents": 1200})[0], 200)
        for category, key, expected in (("training", f"T-{self.tag}", 3000),
                                        ("standby", f"B-{self.tag}", 2400)):
            s, body = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                                    {"confirm_key": key, "worker_id": wid,
                                     "operation_id": op_id,
                                     "started_at": "2026-09-20T09:00:00",
                                     "ended_at": "2026-09-20T11:00:00",
                                     "category": category})
            self.assertEqual(s, 200, body)
            self.assertEqual(body["pay_items"][0]["amount_cents"], expected)
            self.assertEqual(body["pay_items"][0]["category"], category)

    # --- 4. 离线补传 / 重复确认不增加工时 ---
    def test_offline_sync_and_duplicate_confirmation(self):
        wid, _ = self.make_worker(f"阿补-{self.tag}")
        op_id = self.make_operation(f"O-{self.tag}")
        self.qualify(wid, op_id)
        payload = {"worker_id": wid, "scan_code": "GATE9", "event_type": "in",
                   "event_time": "2026-09-20T07:55:00",
                   "received_at": "2026-09-20T12:30:00",
                   "source": "offline_sync", "client_event_id": f"{self.tag}-off"}
        s, first = self.api.call("POST", "/api/punches", self.staff("leader"), payload)
        self.assertEqual((s, first["accepted"], first["duplicate"]), (200, True, False))
        s, second = self.api.call("POST", "/api/punches", self.staff("leader"), payload)
        self.assertEqual((s, second["duplicate"], second["id"]),
                         (200, True, first["id"]))
        # 换一个幂等键但指纹相同，同样拒绝。
        payload["client_event_id"] = f"{self.tag}-off-again"
        s, third = self.api.call("POST", "/api/punches", self.staff("leader"), payload)
        self.assertTrue(third["duplicate"])
        punches = self.api.call("GET", f"/api/workers/{wid}/punches",
                                self.staff("leader"))[1]
        self.assertEqual(len(punches), 1)  # 原始打卡只有一条

        report = {"confirm_key": f"C-{self.tag}", "worker_id": wid,
                  "operation_id": op_id,
                  "started_at": "2026-09-20T08:00:00",
                  "ended_at": "2026-09-20T10:00:00", "category": "regular"}
        s, body1 = self.api.call("POST", "/api/attendance-reports",
                                 self.staff("leader"), report)
        self.assertEqual(s, 200, body1)
        s, body2 = self.api.call("POST", "/api/attendance-reports",
                                 self.staff("leader"), report)
        self.assertEqual(s, 200)
        self.assertTrue(body2["duplicate_confirmation"])
        self.assertEqual([s["id"] for s in body1["segments"]],
                         [s["id"] for s in body2["segments"]])
        segments = self.api.call(
            "GET", f"/api/workers/{wid}/segments", self.staff("leader"))[1]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["minutes"], 120)

    def test_same_punch_cannot_anchor_two_reports(self):
        wid, _ = self.make_worker(f"阿双-{self.tag}")
        op_id = self.make_operation(f"D-{self.tag}")
        self.qualify(wid, op_id)
        p = self.api.call("POST", "/api/punches", self.staff("leader"),
                          {"worker_id": wid, "event_type": "in",
                           "event_time": "2026-09-20T08:00:00",
                           "client_event_id": f"{self.tag}-p"})[1]
        base = {"worker_id": wid, "operation_id": op_id,
                "start_punch_event_id": p["id"]}
        s, b1 = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                              {**base, "confirm_key": f"{self.tag}-a",
                               "started_at": "2026-09-20T08:00:00",
                               "ended_at": "2026-09-20T09:00:00"})
        self.assertEqual(s, 200, b1)
        s, b2 = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                              {**base, "confirm_key": f"{self.tag}-b",
                               "started_at": "2026-09-20T08:00:00",
                               "ended_at": "2026-09-20T10:00:00"})
        self.assertEqual(s, 409)
        self.assertEqual(b2["error"], "punch_already_used")

    # --- 5. 原始打卡不可变、纠正只作新版本取代 ---
    def test_supersede_keeps_raw_history(self):
        wid, _ = self.make_worker(f"阿改-{self.tag}")
        op_id = self.make_operation(f"R-{self.tag}")
        self.qualify(wid, op_id)
        s, old = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                               {"confirm_key": f"OLD-{self.tag}", "worker_id": wid,
                                "operation_id": op_id,
                                "started_at": "2026-09-20T08:00:00",
                                "ended_at": "2026-09-20T12:00:00"})
        self.assertEqual(s, 200, old)
        old_group = old["report_group"]
        s, new = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                               {"confirm_key": f"NEW-{self.tag}", "worker_id": wid,
                                "operation_id": op_id,
                                "started_at": "2026-09-20T08:00:00",
                                "ended_at": "2026-09-20T11:00:00",
                                "supersedes": old_group})
        self.assertEqual(s, 200, new)
        self.assertEqual(new["total_minutes"], 180)

        all_segments = self.api.call(
            "GET", f"/api/workers/{wid}/segments?include_superseded=1",
            self.staff("leader"))[1]
        self.assertEqual(len(all_segments), 2)  # 旧段保留
        current = self.api.call("GET", f"/api/workers/{wid}/segments",
                                self.staff("leader"))[1]
        self.assertEqual(len(current), 1)
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        statuses = {i["status"] for i in payroll["items"]}
        self.assertEqual(statuses, {"active", "reversed"})
        self.assertEqual(payroll["summary"]["payable_cents"], 6000)  # 3 小时 ×20

    # --- 6. 顶班 ---
    def test_substitute_role_marked(self):
        wid, _ = self.make_worker(f"阿顶-{self.tag}")
        absent, _ = self.make_worker(f"阿缺勤-{self.tag}")
        op_id = self.make_operation(f"U-{self.tag}")
        self.qualify(wid, op_id)
        s, shift = self.api.call("POST", "/api/shifts", self.staff("leader"),
                                 {"operation_id": op_id,
                                  "planned_start": "2026-09-20T14:00:00",
                                  "planned_end": "2026-09-20T18:00:00"})
        shift_id = shift["id"]
        s, body = self.api.call(
            "POST", f"/api/shifts/{shift_id}/assignments", self.staff("leader"),
            {"worker_id": wid, "role": "substitute", "replaced_worker_id": absent})
        self.assertEqual(s, 200, body)
        s, report = self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                                  {"confirm_key": f"SUB-{self.tag}", "worker_id": wid,
                                   "operation_id": op_id, "shift_id": shift_id,
                                   "started_at": "2026-09-20T14:00:00",
                                   "ended_at": "2026-09-20T18:00:00"})
        self.assertEqual(s, 200, report)
        self.assertEqual(report["role"], "substitute")
        self.assertIn("顶班", report["pay_items"][0]["note"])

    # --- 7. 计件 + 质检复核：不直接扣款 ---
    def _piece_worker(self):
        wid, wtoken = self.make_worker(f"阿件-{self.tag}")
        op_id = self.make_operation(f"PC-{self.tag}", hourly="20.00", piece="5.00")
        self.qualify(wid, op_id)
        return wid, wtoken, op_id

    def test_quality_return_is_withheld_then_partial_upheld(self):
        wid, _, op_id = self._piece_worker()
        s, out = self.api.call("POST", "/api/production-records", self.staff("leader"),
                               {"worker_id": wid, "operation_id": op_id, "quantity": 100,
                                "work_date": "2026-09-20",
                                "client_event_id": f"{self.tag}-prod"})
        self.assertEqual(s, 200, out)
        record_id = out["id"]
        # 重复提交计件不重复计酬。
        s, again = self.api.call("POST", "/api/production-records", self.staff("leader"),
                                 {"worker_id": wid, "operation_id": op_id, "quantity": 100,
                                  "work_date": "2026-09-20",
                                  "client_event_id": f"{self.tag}-prod"})
        self.assertTrue(again["duplicate"])

        s, case = self.api.call("POST", "/api/quality-cases", self.staff("production"),
                                {"production_record_id": record_id, "quantity": 20,
                                 "reason": "饼皮烤色不均",
                                 "evidence": "qc-photo-1.jpg"})
        self.assertEqual(s, 200, case)
        case_id = case["id"]
        self.assertEqual(case["withheld_cents"], 10000)  # 20 × 5 元暂缓
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        self.assertEqual(payroll["summary"]["withheld_cents"], 10000)
        self.assertEqual(payroll["summary"]["payable_cents"], 40000)  # 未被直接扣款

        # 责任成立（部分退回 20 件）。
        s, decided = self.api.call(
            "POST", f"/api/quality-cases/{case_id}/decision", self.staff("production"),
            {"decision": "upheld", "note": "复核监控与留样确认责任在个人"})
        self.assertEqual(s, 200, decided)
        self.assertEqual(decided["status"], "upheld")
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        # 原项冲销留痕 + 合格 80 件补发 + 退回 20 件负向红冲 → 净额 400 元。
        self.assertEqual(payroll["summary"]["payable_cents"], 40000)
        notes = " ".join(i["note"] for i in payroll["items"])
        self.assertIn("核减", notes)
        self.assertIn("合格数量 80 件", notes)
        self.assertTrue(any(i["amount_cents"] == -10000 for i in payroll["items"]))

    def test_quality_return_rejected_restores_pay(self):
        wid, _, op_id = self._piece_worker()
        record_id = self.api.call("POST", "/api/production-records", self.staff("leader"),
                                  {"worker_id": wid, "operation_id": op_id, "quantity": 50,
                                   "work_date": "2026-09-20",
                                   "client_event_id": f"{self.tag}-prod2"})[1]["id"]
        case_id = self.api.call("POST", "/api/quality-cases", self.staff("production"),
                                {"production_record_id": record_id, "quantity": 50,
                                 "reason": "疑似受潮"})[1]["id"]
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        self.assertEqual(payroll["summary"]["payable_cents"], 0)
        s, body = self.api.call(
            "POST", f"/api/quality-cases/{case_id}/decision", self.staff("production"),
            {"decision": "rejected", "note": "留样检验合格，退回系运输包装问题"})
        self.assertEqual(s, 200, body)
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        self.assertEqual(payroll["summary"]["payable_cents"], 25000)

    def test_quality_rework_flow(self):
        wid, _, op_id = self._piece_worker()
        record_id = self.api.call("POST", "/api/production-records", self.staff("leader"),
                                  {"worker_id": wid, "operation_id": op_id, "quantity": 30,
                                   "work_date": "2026-09-20",
                                   "client_event_id": f"{self.tag}-prod3"})[1]["id"]
        case_id = self.api.call("POST", "/api/quality-cases", self.staff("production"),
                                {"production_record_id": record_id, "quantity": 10,
                                 "reason": "包装漏贴标签"})[1]["id"]
        self.assertEqual(self.api.call(
            "POST", f"/api/quality-cases/{case_id}/decision", self.staff("production"),
            {"decision": "rework", "note": "安排返工重贴"})[0], 200)
        self.assertEqual(self.api.call(
            "POST", f"/api/quality-cases/{case_id}/rework-complete", self.staff("leader"),
            {"note": "重贴完成，复验合格"})[0], 200)
        case = self.api.call("GET", f"/api/quality-cases/{case_id}",
                             self.staff("production"))[1]
        self.assertEqual(case["status"], "rejected")
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                self.staff("leader"))[1]
        self.assertEqual(payroll["summary"]["payable_cents"], 15000)

    # --- 8. 申诉进展可见 ---
    def test_appeal_lifecycle_visible_to_worker(self):
        wid, wtoken = self.make_worker(f"阿申-{self.tag}")
        op_id = self.make_operation(f"A-{self.tag}")
        self.qualify(wid, op_id)
        self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                      {"confirm_key": f"AP-{self.tag}", "worker_id": wid,
                       "operation_id": op_id,
                       "started_at": "2026-09-20T08:00:00",
                       "ended_at": "2026-09-20T10:00:00"})
        item_id = self.api.call("GET", f"/api/workers/{wid}/payroll",
                                wtoken)[1]["items"][0]["id"]
        s, appeal = self.api.call(
            "POST", f"/api/workers/{wid}/appeals", wtoken,
            {"category": "hours", "pay_item_id": item_id,
             "reason": "当晚加班到 22 点，只算了 2 小时"})
        self.assertEqual(s, 200, appeal)
        appeal_id = appeal["id"]
        self.assertEqual(appeal["status"], "submitted")

        for action, role, status, message in (
                ("accept", "hr", "reviewing", "已调取打卡记录核查"),
                ("resolve", "production", "resolved", "确认漏算 1 小时，补录确认单")):
            s, body = self.api.call(
                "POST", f"/api/appeals/{appeal_id}/{action}", self.staff(role),
                {"message": message})
            self.assertEqual(s, 200, body)
            self.assertEqual(body["status"], status)

        mine = self.api.call("GET", f"/api/workers/{wid}/appeals", wtoken)[1]
        self.assertEqual(len(mine), 1)
        messages = [e["message"] for e in mine[0]["events"]]
        self.assertIn("已调取打卡记录核查", messages)
        self.assertIn("确认漏算 1 小时，补录确认单", messages)
        # 薪酬项上带申诉状态。
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll", wtoken)[1]
        self.assertEqual(payroll["items"][0]["appeal"]["status"], "resolved")

    # --- 9. 封账不可悄然修改 + 哈希链 ---
    def test_sealed_batch_tamper_proof_and_chain(self):
        wid, _ = self.make_worker(f"阿财-{self.tag}")
        op_id = self.make_operation(f"F-{self.tag}")
        self.qualify(wid, op_id)
        self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                      {"confirm_key": f"FIN-{self.tag}", "worker_id": wid,
                       "operation_id": op_id,
                       "started_at": "2026-09-20T08:00:00",
                       "ended_at": "2026-09-20T11:00:00"})
        s, batch = self.api.call("POST", "/api/payment-batches/seal",
                                 self.staff("finance"),
                                 {"period_start": "2026-09-20",
                                  "period_end": "2026-09-20"})
        self.assertEqual(s, 200, batch)
        self.assertEqual(batch["total_cents"], 6000)
        self.assertTrue(batch["chain_valid"])
        batch_id = batch["id"]

        # 已封账的薪酬项不能再改；触发器在数据库层直接拒绝。
        import sqlite3
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute(
                "UPDATE pay_items SET amount_cents=1 WHERE batch_id=?", (batch_id,))
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute("DELETE FROM payments WHERE batch_id=?", (batch_id,))
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute(
                "UPDATE payment_batches SET total_cents=1 WHERE id=?", (batch_id,))
        # 原始打卡：触发器只在命中行时触发，先造一条再验证禁删/禁改。
        self.api.call("POST", "/api/punches", self.staff("leader"),
                      {"worker_id": wid, "event_type": "in",
                       "event_time": "2026-09-21T07:55:00",
                       "client_event_id": f"raw-{self.tag}"})
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute("DELETE FROM punch_events")
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute(
                "UPDATE punch_events SET event_time='2026-09-21T09:00:00'")

        # 再次封账不重复纳入。
        s, body = self.api.call("POST", "/api/payment-batches/seal",
                                self.staff("finance"),
                                {"period_start": "2026-09-20",
                                 "period_end": "2026-09-20"})
        self.assertEqual(s, 409)
        self.assertEqual(body["error"], "empty_batch")

        # 第二批次形成哈希链。
        self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                      {"confirm_key": f"FIN2-{self.tag}", "worker_id": wid,
                       "operation_id": op_id,
                       "started_at": "2026-09-21T08:00:00",
                       "ended_at": "2026-09-21T09:00:00"})
        s, batch2 = self.api.call("POST", "/api/payment-batches/seal",
                                  self.staff("finance"),
                                  {"period_start": "2026-09-21",
                                   "period_end": "2026-09-21"})
        self.assertEqual(s, 200, batch2)
        self.assertIsNotNone(batch2["prev_hash"])
        verify = self.api.call("GET", "/api/payment-batches/verify-chain",
                               self.staff("finance"))[1]
        self.assertTrue(verify["valid"])
        self.assertEqual(len(verify["batches"]), 2)

        # 封账后班次锁定。
        shifts = self.api.call("GET", "/api/shifts?date=2026-09-20",
                               self.staff("leader"))[1]
        self.assertTrue(all(sh["locked"] for sh in shifts))

    # --- 10. 就业统计 ---
    def test_employment_stats(self):
        w1, _ = self.make_worker(f"阿统1-{self.tag}")
        w2, _ = self.make_worker(f"阿统2-{self.tag}")
        op_id = self.make_operation(f"ST-{self.tag}")
        self.qualify(w1, op_id)
        self.qualify(w2, op_id)
        for wid, key, end in ((w1, "s1", "10:00:00"), (w2, "s2", "11:30:00")):
            self.api.call("POST", "/api/attendance-reports", self.staff("leader"),
                          {"confirm_key": f"{key}-{self.tag}", "worker_id": wid,
                           "operation_id": op_id,
                           "started_at": "2026-09-20T08:00:00",
                           "ended_at": f"2026-09-20T{end}"})
        self.api.call("POST", "/api/payment-batches/seal", self.staff("finance"),
                      {"period_start": "2026-09-20", "period_end": "2026-09-20"})
        s, stats = self.api.call(
            "GET", "/api/stats/employment?from=2026-09-20&to=2026-09-20",
            self.staff("admin"))
        self.assertEqual(s, 200, stats)
        self.assertGreaterEqual(stats["employed_workers"], 2)
        self.assertGreaterEqual(stats["effective_work"]["total_hours"], 3.5)
        self.assertGreaterEqual(stats["paid"]["workers"], 2)
        self.assertGreater(stats["paid"]["total_cents"], 0)

    # --- 11. 鉴权与数据隔离 ---
    def test_auth_and_owner_isolation(self):
        wid, wtoken = self.make_worker(f"阿权-{self.tag}")
        other, other_token = self.make_worker(f"阿权邻居-{self.tag}")
        # 无令牌。
        self.assertEqual(self.api.call("GET", "/api/workers")[0], 401)
        # 村民不能列人员名册。
        self.assertEqual(self.api.call("GET", "/api/workers", wtoken)[0], 403)
        # 村民不能替他人建资质。
        self.assertEqual(self.api.call(
            "POST", f"/api/workers/{other}/trainings", wtoken,
            {"valid_until": "2026-12-31"})[0], 403)
        # 村民可以看本人薪酬，不能看他人。
        self.assertEqual(self.api.call("GET", f"/api/workers/{wid}/payroll",
                                       wtoken)[0], 200)
        self.assertEqual(self.api.call("GET", f"/api/workers/{other}/payroll",
                                       wtoken)[0], 403)
        # 村民不能封账。
        self.assertEqual(self.api.call("POST", "/api/payment-batches/seal", wtoken,
                                       {"period_start": "2026-09-20",
                                        "period_end": "2026-09-20"})[0], 403)
        # 班组长不能封账。
        self.assertEqual(self.api.call("POST", "/api/payment-batches/seal",
                                       self.staff("leader"),
                                       {"period_start": "2026-09-20",
                                        "period_end": "2026-09-20"})[0], 403)


    # --- 12. 人工调整必须留痕 ---
    def test_manual_adjustment_is_a_visible_line_item(self):
        wid, wtoken = self.make_worker(f"阿调-{self.tag}")
        s, adj = self.api.call(
            "POST", f"/api/workers/{wid}/pay-adjustments", self.staff("finance"),
            {"work_date": "2026-09-20", "amount_yuan": "-12.50",
             "note": "误餐补已发现金，账上核减"})
        self.assertEqual(s, 200, adj)
        self.assertEqual(adj["amount_cents"], -1250)
        payroll = self.api.call("GET", f"/api/workers/{wid}/payroll", wtoken)[1]
        self.assertEqual(payroll["summary"]["payable_cents"], -1250)
        self.assertEqual(payroll["items"][0]["note"], "误餐补已发现金，账上核减")
        # 村民不能自行调整。
        self.assertEqual(self.api.call(
            "POST", f"/api/workers/{wid}/pay-adjustments", wtoken,
            {"work_date": "2026-09-20", "amount_yuan": "100", "note": "x"})[0], 403)
        # 数据库层依旧禁止删除调整项。
        import sqlite3
        with self.assertRaises(sqlite3.Error):
            self.service.db.execute("DELETE FROM pay_items WHERE id=?", (adj["id"],))

    def test_worker_can_view_own_quality_cases(self):
        wid, wtoken = self.make_worker(f"阿案-{self.tag}")
        op_id = self.make_operation(f"QCW-{self.tag}", piece="3.00")
        self.qualify(wid, op_id)
        record_id = self.api.call("POST", "/api/production-records", self.staff("leader"),
                                  {"worker_id": wid, "operation_id": op_id, "quantity": 10,
                                   "work_date": "2026-09-20",
                                   "client_event_id": f"{self.tag}-q"})[1]["id"]
        case_id = self.api.call("POST", "/api/quality-cases", self.staff("production"),
                                {"production_record_id": record_id, "quantity": 4,
                                 "reason": "馅料外溢"})[1]["id"]
        s, cases = self.api.call("GET", f"/api/workers/{wid}/quality-cases", wtoken)
        self.assertEqual(s, 200, cases)
        self.assertEqual([c["id"] for c in cases], [case_id])
        self.assertEqual(cases[0]["events"][0]["event_type"], "open")


if __name__ == "__main__":
    unittest.main()
