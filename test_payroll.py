"""端到端测试：覆盖资质闸门、编排顶班、跨午夜/休息/停机拆分、

打卡去重与不可篡改、质检退回责任复核、申诉、封账批次与季节统计。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from commands import (
    ConflictError,
    DomainError,
    NotFoundError,
    PayService,
)
from events import EventStore
from rules import (
    SEG_NIGHT,
    Interval,
    PieceRecord,
    Rules,
    build_daily_line,
    hours_between,
    normalize_intervals,
    parse_ts,
    split_at_midnight,
    split_by_night,
    subtract_breaks,
)

A = "leader:王班长"
ADMIN = "admin:人事"
QC = "qc:质检员"
SC = "scanner:gate1"
FIN = "finance:财务"


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.svc = PayService(EventStore())
        self._seed_workers()

    def _seed_workers(self):
        s = self.svc
        s.register_worker("w1", "阿梅", ADMIN)
        s.register_worker("w2", "阿兰", ADMIN)
        s.register_worker("w3", "阿珠", ADMIN)
        for wid in ("w1", "w2"):
            s.record_training(wid, "food_safety", "2026-09-01 09:00", ADMIN)
            s.record_health_cert(wid, "2027-09-01", ADMIN)
        s.create_operation("op_time", ADMIN, name="烤炉", hourly_wage=24,
                           required_trainings=["food_safety"])
        s.create_operation("op_piece", ADMIN, name="包装", piece_unit_pay=1,
                           required_trainings=["food_safety"])

    # ---------------- 规则纯函数 ----------------

    def test_midnight_and_night_split(self):
        iv = Interval("2026-09-19 22:00", "2026-09-20 05:00")
        pieces = split_at_midnight(iv)
        self.assertEqual([p.start[:10] for p in pieces], ["2026-09-19", "2026-09-20"])
        night = split_by_night(pieces[1], Rules())
        self.assertTrue(all(p.kind == SEG_NIGHT for p in night))
        self.assertAlmostEqual(hours_between(parse_ts("2026-09-19 22:00"),
                                             parse_ts("2026-09-20 05:00")), 7.0)

    def test_break_is_subtracted(self):
        work = normalize_intervals(
            [Interval("2026-09-20 08:00", "2026-09-20 12:00")], Rules())
        kept = subtract_breaks(work, [Interval("2026-09-20 10:00", "2026-09-20 10:30")])
        total = sum(hours_between(parse_ts(s.start), parse_ts(s.end)) for s in kept)
        self.assertAlmostEqual(total, 3.5)

    def test_overlap_rejected(self):
        with self.assertRaises(ValueError):
            normalize_intervals([
                Interval("2026-09-20 08:00", "2026-09-20 10:00"),
                Interval("2026-09-20 09:00", "2026-09-20 11:00"),
            ], Rules())

    def test_piece_guarantee_only_covers_piece_segments(self):
        # 1 小时培训等待（0.5 薪）+ 2 小时计件在岗：保底只比计件段 2 小时
        pieces = [PieceRecord(id="q1", operation_id="op_piece", quantity=3,
                              unit_pay=1, status="accepted")]
        line = build_daily_line(
            "w1", "2026-09-20",
            [Interval("2026-09-20 07:00", "2026-09-20 08:00", "training_wait"),
             Interval("2026-09-20 08:00", "2026-09-20 10:00", "piece",
                      meta={"piece": True})],
            [], pieces, [], Rules())
        # 等待 10 + 计件段保底 40（高于计件 3）= 50
        self.assertEqual(line.gross_amount, 50.0)

    # ---------------- 资质闸门 ----------------

    def test_eligibility_blocks_without_training_or_cert(self):
        self.svc.record_health_cert("w3", "2027-09-01", ADMIN)  # 有证无培训
        result = self.svc.check_eligibility("w3", "op_time", "2026-09-20 08:00")
        self.assertFalse(result["eligible"])
        self.assertTrue(any("培训" in p for p in result["problems"]))

        self.svc.record_training("w3", "food_safety", "2026-09-01 09:00", ADMIN)
        # 再登记一本已过期的新证：以最新一本为准，应判过期
        self.svc.record_health_cert("w3", "2026-09-10", ADMIN)
        result = self.svc.check_eligibility("w3", "op_time", "2026-09-20 08:00")
        self.assertFalse(result["eligible"])
        self.assertTrue(any("健康证明已过期" in p for p in result["problems"]))

    def test_assign_requires_eligibility(self):
        self.svc.create_shift("sh1", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        with self.assertRaises(DomainError):
            self.svc.assign_worker("sh1", "w3", A)  # w3 无培训无证
        event = self.svc.assign_worker("sh1", "w1", A)
        self.assertEqual(event["type"], "worker_assigned")

    def test_double_booking_rejected(self):
        self.svc.create_shift("a", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        self.svc.create_shift("b", "op_time", "2026-09-20 12:00",
                              "2026-09-20 20:00", A)
        self.svc.assign_worker("a", "w1", A)
        with self.assertRaises(ConflictError):
            self.svc.assign_worker("b", "w1", A)

    # ---------------- 打卡去重与原始保留 ----------------

    def test_offline_upload_and_leader_confirm_do_not_add_hours(self):
        self.svc.create_shift("sh1", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        self.svc.assign_worker("sh1", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC,
                              client_event_id="DEV-001")
        self.svc.record_punch("w1", "out", "2026-09-20 16:00", SC,
                              client_event_id="DEV-002")
        # 扫码器离线补传同一事件号
        with self.assertRaises(ConflictError):
            self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC,
                                  source="offline_upload", client_event_id="DEV-001")
        # 班组长重复确认（不同事件号但同人同向同刻）
        with self.assertRaises(ConflictError):
            self.svc.record_punch("w1", "in", "2026-09-20 08:00", A,
                                  source="leader_confirm", client_event_id="LEAD-9")
        pay = self.svc.worker_payroll("w1", "2026-09-20", "2026-09-20")
        self.assertEqual(pay["total_hours"], 8.0)
        self.assertEqual(len(pay["raw_punches"]), 2)  # 原始打卡只有两条

    def test_punch_correction_keeps_raw_record(self):
        self.svc.create_shift("sh1", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        self.svc.assign_worker("sh1", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:05", SC, punch_id="p_in")
        self.svc.record_punch("w1", "out", "2026-09-20 15:55", SC, punch_id="p_out")
        self.svc.correct_punches("w1", ["p_in", "p_out"],
                                 "2026-09-20 08:00", "2026-09-20 16:00", A,
                                 reason="扫码器时间漂移 5 分钟")
        pay = self.svc.worker_payroll("w1", "2026-09-20", "2026-09-20")
        self.assertEqual(pay["total_hours"], 8.0)
        raw = {p["punch_id"]: p for p in pay["raw_punches"]}
        self.assertIsNotNone(raw["p_in"]["corrected_by"])  # 原始记录仍在且被标记
        self.assertEqual(len(pay["corrections"]), 1)

    # ---------------- 中断与拆分 ----------------

    def test_break_down_and_cross_midnight_pay(self):
        # 白班 w1：8h 窗口，30min 无薪休息，1h 停机保底
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        self.svc.record_punch("w1", "out", "2026-09-20 16:00", SC)
        self.svc.record_incident("day", "w1", "break",
                                 "2026-09-20 12:00", "2026-09-20 12:30", A)
        self.svc.record_incident("day", "w1", "equipment_down",
                                 "2026-09-20 14:00", "2026-09-20 15:00", A)
        pay = self.svc.worker_payroll("w1", "2026-09-20", "2026-09-20")
        line = pay["workers"][0]["lines"][0]
        self.assertEqual(line["hours_by_kind"]["regular"], 6.5)
        self.assertEqual(line["hours_by_kind"]["down"], 1.0)
        # 6.5h × 24 + 1h × 24 × 0.7
        self.assertAlmostEqual(line["gross_amount"], 156 + 16.8, places=2)

    def test_training_wait_before_shift_pays_when_punched_not_scheduled(self):
        # w2 到岗等待上岗培训：07:00–08:00 打卡但未排岗
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w2", "in", "2026-09-20 07:00", SC)
        self.svc.record_punch("w2", "out", "2026-09-20 08:00", SC)
        self.svc.record_incident("day", "w2", "training_wait",
                                 "2026-09-20 07:00", "2026-09-20 08:00", A)
        pay = self.svc.worker_payroll("w2", "2026-09-20", "2026-09-20")
        line = pay["workers"][0]["lines"][0]
        self.assertEqual(line["hours_by_kind"]["training_wait"], 1.0)
        self.assertAlmostEqual(line["gross_amount"], 10.0, places=2)

    def test_wait_without_punch_is_flagged_not_paid(self):
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        self.svc.record_punch("w1", "out", "2026-09-20 12:00", SC)
        self.svc.record_incident("day", "w2", "training_wait",
                                 "2026-09-20 07:00", "2026-09-20 08:00", A)
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        self.assertEqual([w["worker_id"] for w in pay["workers"]], ["w1"])
        self.assertTrue(any(i["worker_id"] == "w2" for i in pay["unattested_incidents"]))

    def test_unpaired_and_unscheduled_punches_are_surfaced(self):
        # w1 只打上班卡：封账必须拦截
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        self.assertTrue(any(i["kind"] == "unpaired_punch"
                            for i in pay["unattested_incidents"]))
        self.assertEqual(pay["workers"], [])  # 未配对不产生工时
        with self.assertRaises(DomainError):
            self.svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN)
        # 更正补齐后可封账
        punch_id = pay["unattested_incidents"][0]["punch_id"]
        self.svc.correct_punches("w1", [punch_id], "2026-09-20 08:00",
                                 "2026-09-20 12:00", A, reason="下班卡漏刷，监控核实")
        batch = self.svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN)
        self.assertEqual(batch["total_amount"], 96.0)

    def test_unscheduled_punch_is_flagged_not_paid(self):
        # w2 有打卡但当天未被编入任何班次
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w2", "in", "2026-09-20 09:00", SC)
        self.svc.record_punch("w2", "out", "2026-09-20 11:00", SC)
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        flagged = [i for i in pay["unattested_incidents"]
                   if i["kind"] == "unscheduled_punch"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual([w["worker_id"] for w in pay["workers"]], [])

    # ---------------- 顶班/换岗 ----------------

    def test_substitution_pays_replacement_only_in_window(self):
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 16:00", A)
        asg = self.svc.assign_worker("day", "w1", A)["payload"]["assignment_id"]
        # w2 顶班 10:00–13:00
        self.svc.substitute_worker("day", asg, "w2", "2026-09-20 10:00",
                                   "2026-09-20 13:00", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        self.svc.record_punch("w1", "out", "2026-09-20 16:00", SC)
        self.svc.record_punch("w2", "in", "2026-09-20 10:00", SC)
        self.svc.record_punch("w2", "out", "2026-09-20 13:00", SC)
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        hours = {w["worker_id"]: w["total_hours"] for w in pay["workers"]}
        self.assertEqual(hours["w1"], 5.0)   # 8 - 3
        self.assertEqual(hours["w2"], 3.0)   # 仅顶班窗口

    # ---------------- 计件与质检责任复核 ----------------

    def test_qc_rejection_goes_to_review_not_direct_deduction(self):
        self.svc.create_shift("day", "op_piece", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        self.svc.record_punch("w1", "out", "2026-09-20 12:00", SC)
        self.svc.report_quantity("w1", "op_piece", "day", 100, QC,
                                 quantity_id="q1")
        # 退回必须有证据
        with self.assertRaises(DomainError):
            self.svc.qc_decide("q1", "rejected", QC)
        self.svc.qc_decide("q1", "rejected", QC, evidence="照片IMG-1：封口不牢")
        self.svc.open_review("q1", QC)
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        line = pay["workers"][0]["lines"][0]
        # 复核未结论：按原额 100 暂计，不直接扣
        self.assertEqual(line["piece_amount"], 100.0)
        self.assertEqual(pay["provisional_items"][0]["quantity_id"], "q1")

    def test_review_conclusions_set_pay(self):
        self._piece_shift_rejected("qA")
        self.svc.open_review("qA", QC)
        self.svc.conclude_review(
            self.svc.list_reviews()[0]["review_id"], "non_worker", QC,
            note="设备温控故障导致", evidence="维修单R-1")
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        self.assertEqual(pay["workers"][0]["lines"][0]["piece_amount"], 100.0)

        # 个人责任的另一件
        self._piece_shift_rejected("qB", worker="w2")
        self.svc.open_review("qB", QC, review_id="revB")
        self.svc.conclude_review("revB", "worker", QC, accepted_qty=60,
                                 note="操作不当", evidence="监控片段V-2")
        pay = self.svc.compute_payroll("2026-09-20", "2026-09-20")
        w2 = next(w for w in pay["workers"] if w["worker_id"] == "w2")
        self.assertEqual(w2["lines"][0]["piece_amount"], 60.0)

    def _piece_shift_rejected(self, qid, worker="w1"):
        sid = f"sh_{qid}"
        self.svc.create_shift(sid, "op_piece", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker(sid, worker, A)
        self.svc.record_punch(worker, "in", "2026-09-20 08:00", SC)
        self.svc.record_punch(worker, "out", "2026-09-20 12:00", SC)
        self.svc.report_quantity(worker, "op_piece", sid, 100, QC, quantity_id=qid)
        self.svc.qc_decide(qid, "rejected", QC, evidence=f"证据-{qid}")

    def test_post_close_review_creates_evidenced_adjustment_next_batch(self):
        self._piece_shift_rejected("qC")
        self.svc.open_review("qC", QC, review_id="revC")
        batch = self.svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN,
                                       allow_provisional=True)
        first_total = batch["total_amount"]
        self.assertEqual(first_total, 100.0)  # 暂计 100
        # 封账后结论个人责任：旧批次不变，产生 -100 调整进入下一批
        self.svc.conclude_review("revC", "worker", QC, accepted_qty=0,
                                 note="个人操作责任", evidence="证据链E-9")
        verify = self.svc.verify_batch(batch["batch_id"])
        self.assertTrue(verify["ok"])
        self.assertTrue(any(e["type"] == "review_concluded"
                            for e in verify["post_close_related_events"]))
        # 下一批（连续日期）包含负向调整
        next_pay = self.svc.compute_payroll("2026-09-21", "2026-09-21")
        adj_line = next(l for w in next_pay["workers"] for l in w["lines"]
                        if l["work_date"] == "2026-09-20")
        self.assertEqual(adj_line["gross_amount"], -100.0)
        self.assertTrue(adj_line["adjustments"][0]["evidence"])

    # ---------------- 申诉 ----------------

    def test_appeal_grant_is_evidenced_and_visible(self):
        self.svc.open_appeal("w1", "夜班时长少算", "worker:w1",
                             appeal_id="apl1")
        with self.assertRaises(DomainError):
            self.svc.resolve_appeal("apl1", "granted", ADMIN, grant_amount=40)  # 缺说明
        self.svc.resolve_appeal("apl1", "granted", ADMIN, note="核对监控属实，补发",
                                grant_amount=40, work_date="2026-09-20")
        pay = self.svc.worker_payroll("w1", "2026-09-20", "2026-09-20")
        self.assertEqual(pay["workers"][0]["lines"][0]["gross_amount"], 40.0)
        appeal = pay["appeals"][0]
        self.assertEqual(appeal["status"], "resolved")
        self.assertEqual(appeal["decision"], "granted")

    # ---------------- 封账与不可篡改 ----------------

    def test_close_continuity_and_immutability(self):
        self.svc.create_shift("day", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("day", "w1", A)
        self.svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
        self.svc.record_punch("w1", "out", "2026-09-20 12:00", SC)
        self.svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN)
        # 不得重封/跳封
        with self.assertRaises(DomainError):
            self.svc.close_payroll("重复", "2026-09-20", "2026-09-20", FIN)
        with self.assertRaises(DomainError):
            self.svc.close_payroll("跳封", "2026-09-23", "2026-09-23", FIN)
        # 已封账日不允许直接改打卡
        with self.assertRaises(DomainError):
            self.svc.correct_punches("w1", [], "2026-09-20 08:00",
                                     "2026-09-20 11:00", A, reason="x")

    def test_disk_tampering_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            svc = PayService(EventStore(path))
            svc.register_worker("w1", "阿梅", ADMIN)
            svc.record_training("w1", "food_safety", "2026-09-01 09:00", ADMIN)
            svc.record_health_cert("w1", "2027-09-01", ADMIN)
            svc.create_operation("op_time", ADMIN, hourly_wage=24,
                                 required_trainings=["food_safety"])
            svc.create_shift("day", "op_time", "2026-09-20 08:00",
                             "2026-09-20 12:00", A)
            svc.assign_worker("day", "w1", A)
            svc.record_punch("w1", "in", "2026-09-20 08:00", SC)
            svc.record_punch("w1", "out", "2026-09-20 12:00", SC)
            svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN)

            # 追加伪造行：序号对不上
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"seq": 1}, ensure_ascii=False) + "\n")
            result = EventStore.verify_disk(path)
            self.assertFalse(result["ok"])

            # 删除伪造行并篡改既有行的载荷字节：内容哈希不符（第 1 行含姓名）
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            lines = lines[:-1]
            self.assertIn("阿梅", lines[0])
            lines[0] = lines[0].replace("阿梅", "阿梅X")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            result = EventStore.verify_disk(path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["broken_at"], 1)

    # ---------------- 季节性统计 ----------------

    def test_seasonal_stats_counts_people_hours_paid(self):
        self.svc.create_operation("op_fixed", ADMIN, name="常年岗", hourly_wage=20,
                                  seasonal=False)
        self.svc.create_shift("s1", "op_time", "2026-09-20 08:00",
                              "2026-09-20 12:00", A)
        self.svc.assign_worker("s1", "w1", A)
        self.svc.create_shift("s2", "op_fixed", "2026-09-20 13:00",
                              "2026-09-20 17:00", A)
        self.svc.assign_worker("s2", "w2", A)
        for wid, out_time in (("w1", "12:00"), ("w2", "17:00")):
            self.svc.record_punch(wid, "in", f"2026-09-20 {('08' if wid=='w1' else '13')}:00", SC)
            self.svc.record_punch(wid, "out", f"2026-09-20 {out_time}", SC)
        self.svc.close_payroll("第一批", "2026-09-20", "2026-09-20", FIN)
        stats = self.svc.seasonal_stats("2026-09-01", "2026-09-30")
        self.assertEqual(stats["seasonal_employment_count"], 1)  # 仅节令岗 w1
        self.assertEqual(stats["effective_hours"], 4.0)
        self.assertEqual(stats["paid_amount"], 4 * 24)


class HttpContractCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from api import make_handler
        from service import build_api, build_service
        cls.service = build_service(None)
        cls.api = build_api(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.api))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _call(self, method, path, token=None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            return error.code, json.load(error)

    @classmethod
    def _seed(cls):
        s = cls.service
        s.register_worker("h1", "村民甲", "admin:t")
        s.record_training("h1", "food_safety", "2026-09-01 09:00", "admin:t")
        s.record_health_cert("h1", "2027-09-01", "admin:t")
        s.create_operation("op_time", "admin:t", hourly_wage=24,
                           required_trainings=["food_safety"])
        s.create_shift("sh1", "op_time", "2026-09-20 08:00",
                       "2026-09-20 12:00", "leader:t")
        s.assign_worker("sh1", "h1", "leader:t")
        s.record_punch("h1", "in", "2026-09-20 08:00", "scanner:t")
        s.record_punch("h1", "out", "2026-09-20 12:00", "scanner:t")

    def test_01_health(self):
        from service import health_payload
        status, body = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_02_auth_and_isolation(self):
        self._seed()
        status, _ = self._call("GET", "/admin/workers")
        self.assertEqual(status, 401)
        status, _ = self._call("GET", "/admin/workers", token="dev-finance-token")
        self.assertEqual(status, 403)
        # 村民只能看本人
        status, _ = self._call(
            "GET", "/workers/h1/payroll?period_start=2026-09-20&period_end=2026-09-20",
            token="worker-other")
        self.assertEqual(status, 403)
        status, body = self._call(
            "GET", "/workers/h1/payroll?period_start=2026-09-20&period_end=2026-09-20",
            token="worker-h1")
        self.assertEqual(status, 200)
        self.assertEqual(body["total_amount"], 96.0)

    def test_03_close_and_worker_sees_batch(self):
        status, body = self._call(
            "POST", "/finance/payroll/close", token="dev-finance-token",
            body={"name": "HTTP批次", "period_start": "2026-09-20",
                  "period_end": "2026-09-20"})
        self.assertEqual(status, 201)
        batch_id = body["batch"]["batch_id"]
        status, verify = self._call(
            "GET", f"/finance/payroll/batches/{batch_id}/verify",
            token="dev-finance-token")
        self.assertEqual(status, 200)
        self.assertTrue(verify["ok"])
        status, view = self._call(
            "GET", "/workers/h1/payroll?period_start=2026-09-20&period_end=2026-09-20",
            token="worker-h1")
        self.assertEqual(status, 200)
        self.assertEqual(view["paid_batches"][0]["batch_id"], batch_id)
        # 封账后逐项明细仍可见，且为冻结值
        self.assertEqual(view["closed_lines"][0]["gross_amount"], 96.0)
        self.assertEqual(view["closed_amount"], 96.0)

    def test_04_unknown_route_404(self):
        status, _ = self._call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_05_offline_reupload_is_rejected_over_http(self):
        payload = {"worker_id": "h1", "dir": "in", "ts": "2026-09-20 07:59",
                   "client_event_id": "SCANNER-42"}
        status, _ = self._call("POST", "/scanner/punches",
                               token="dev-scanner-token", body=payload)
        self.assertEqual(status, 201)
        # 扫码器离线后补传同一事件号
        status, body = self._call("POST", "/scanner/punches",
                                  token="dev-scanner-token",
                                  body={**payload, "source": "offline_upload"})
        self.assertEqual(status, 409)
        self.assertIn("工时不重复计算", body["error"])


if __name__ == "__main__":
    unittest.main()
