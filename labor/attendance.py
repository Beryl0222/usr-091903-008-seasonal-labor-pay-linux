"""打卡接入与出勤确认。

两条写入通道：

* 扫码器/代打卡写入原始事件（在线或离线补传），原始事件只增不改不删，
  同一条物理事件通过 ``client_event_id`` 或 人+设备+方向+时间+来源 指纹去重；
* 班组长按确认单（``confirm_key`` 幂等）申报工作区间，系统按跨午夜与休息中断
  规则拆段并写入小时类薪酬项。重复确认返回首次结果，绝不增加工时。

纠正历史只能"新版本取代"：以新 ``confirm_key`` 携带 ``supersedes`` 提交，
旧段与旧薪酬项保留并标记，绝不删除。
"""

import json
import sqlite3

from .errors import bad_request, conflict, not_found
from .splitting import minutes_between, split_interval
from .timeutil import amount_for_minutes, cents_to_yuan, day_of, fmt, now_ts, parse_ts

RATE_SETTINGS = {
    "training": "training_allowance_cents_per_hour",
    "standby": "standby_guarantee_cents_per_hour",
    "rework": "rework_rate_cents_per_hour",
    "regular": None,
    "piecework": None,  # 计件岗工时不按时计酬，报酬来自产量
}
CATEGORIES = tuple(RATE_SETTINGS)


class Attendance:
    def __init__(self, db):
        self.db = db

    # --- 原始打卡 ---
    def record_punch(self, data, actor):
        worker = self.db.get("workers", data.get("worker_id"))
        if worker is None:
            raise not_found("人员不存在")
        event_type = data.get("event_type")
        if event_type not in ("in", "out"):
            raise bad_request("event_type 只能是 in 或 out")
        event_time = fmt(parse_ts(data.get("event_time")))
        scan_code = data.get("scan_code") or "manual"
        source = data.get("source", "online")
        if source not in ("online", "offline_sync"):
            raise bad_request("source 只能是 online 或 offline_sync")
        client_event_id = data.get("client_event_id")
        received = data.get("received_at")
        received_at = fmt(parse_ts(received)) if received else now_ts()
        if source == "offline_sync" and parse_ts(received_at) < parse_ts(event_time):
            raise bad_request("离线补传的接收时间不能早于打卡时间")

        # 先查幂等：重复补传/重复提交直接返回首次记录，不再新增。
        if client_event_id:
            existing = self.db.query_one(
                "SELECT * FROM punch_events WHERE client_event_id=?", (client_event_id,)
            )
            if existing:
                return {"accepted": False, "duplicate": True, "id": existing["id"],
                        "message": "打卡事件已存在，未重复计入"}
        existing = self.db.query_one(
            """SELECT * FROM punch_events
               WHERE worker_id=? AND scan_code=? AND event_type=? AND event_time=? AND source=?""",
            (worker["id"], scan_code, event_type, event_time, source),
        )
        if existing:
            return {"accepted": False, "duplicate": True, "id": existing["id"],
                    "message": "同一打卡已接收，未重复计入"}

        try:
            punch_id = self.db.insert(
                "punch_events",
                worker_id=worker["id"],
                scan_code=scan_code,
                event_type=event_type,
                event_time=event_time,
                received_at=received_at,
                source=source,
                client_event_id=client_event_id,
                confirmed_by=actor if actor.startswith("leader:") else None,
                created_at=now_ts(),
            )
        except sqlite3.IntegrityError:
            raise conflict("打卡事件幂等键冲突")
        self.db.audit(actor, "record_punch", "punch_events", punch_id,
                      {"worker_id": worker["id"], "source": source})
        return {"accepted": True, "duplicate": False, "id": punch_id,
                "event_time": event_time, "source": source}

    def list_punches(self, worker_id=None, day_from=None, day_to=None):
        sql = "SELECT * FROM punch_events WHERE 1=1"
        params = []
        if worker_id is not None:
            sql += " AND worker_id=?"
            params.append(worker_id)
        if day_from:
            sql += " AND event_time>=?"
            params.append(day_from + "T00:00:00")
        if day_to:
            sql += " AND event_time<=?"
            params.append(day_to + "T23:59:59")
        return [dict(r) for r in self.db.query(sql + " ORDER BY event_time", params)]

    # --- 班组长确认 ---
    def submit_report(self, data, actor):
        confirm_key = (data.get("confirm_key") or "").strip()
        if not confirm_key:
            raise bad_request("confirm_key 必填，用于防重复确认")
        cached = self._cached_report(confirm_key)
        if cached is not None:
            cached["duplicate_confirmation"] = True
            cached["message"] = "该确认单已处理，未重复计算工时"
            return cached

        worker_id = data.get("worker_id")
        worker = self.db.get("workers", worker_id) if isinstance(worker_id, int) else None
        if worker is None:
            raise not_found("人员不存在")
        operation = self.db.get("operations", data.get("operation_id"))
        if operation is None:
            raise not_found("工序不存在")
        category = data.get("category", "regular")
        if category not in CATEGORIES:
            raise bad_request(f"category 必须是 {CATEGORIES} 之一")
        start = parse_ts(data.get("started_at"))
        end = parse_ts(data.get("ended_at"))
        if end <= start:
            raise bad_request("结束时间必须晚于开始时间")

        shift_id = data.get("shift_id")
        role = "normal"
        if shift_id is not None:
            shift = self.db.get("shifts", shift_id)
            if shift is None:
                raise not_found("班次不存在")
            if shift["operation_id"] != operation["id"]:
                raise bad_request("确认的工序与排班工序不一致")
            assignment = self.db.query_one(
                "SELECT * FROM shift_assignments WHERE shift_id=? AND worker_id=?",
                (shift_id, worker_id),
            )
            if assignment is None:
                raise bad_request("该人员未编入此班次，不能确认出勤")
            role = assignment["role"]

        punches = self._validate_punch_refs(
            worker_id, data.get("start_punch_event_id"),
            data.get("end_punch_event_id"), data.get("supersedes"))

        superseded_group = None
        if data.get("supersedes"):
            superseded_group = self._supersede(data["supersedes"], worker_id, actor)

        pieces = split_interval(start, end, data.get("breaks"))
        group = confirm_key
        created_at = now_ts()
        segments = []
        pay_items = []
        first_segment_id = None
        for piece_start, piece_end in pieces:
            minutes = minutes_between(piece_start, piece_end)
            segment_id = self.db.insert(
                "attendance_segments",
                worker_id=worker_id,
                shift_id=shift_id,
                operation_id=operation["id"],
                segment_date=day_of(piece_start),
                started_at=fmt(piece_start),
                ended_at=fmt(piece_end),
                minutes=minutes,
                category=category,
                role=role,
                payable=1,
                report_group=group,
                supersedes_group=superseded_group,
                created_at=created_at,
            )
            if first_segment_id is None:
                first_segment_id = segment_id
            if punches[0]:
                self.db.insert("attribution_links", segment_id=segment_id,
                               punch_event_id=punches[0], role="start")
            if punches[1]:
                self.db.insert("attribution_links", segment_id=segment_id,
                               punch_event_id=punches[1], role="end")
            seg = dict(self.db.get("attendance_segments", segment_id))
            segments.append(seg)
            if category != "piecework" and minutes > 0:
                pay_items.append(self._create_hourly_item(
                    worker_id, operation, category, segment_id, seg, actor))

        if superseded_group:
            self._mark_superseded(superseded_group, group, first_segment_id, worker_id, actor)

        result = {
            "confirm_key": confirm_key,
            "worker_id": worker_id,
            "operation_id": operation["id"],
            "category": category,
            "role": role,
            "report_group": group,
            "superseded_group": superseded_group,
            "segments": [self._segment_json(s) for s in segments],
            "pay_items": pay_items,
            "total_minutes": sum(s["minutes"] for s in segments),
            "duplicate_confirmation": False,
        }
        self.db.insert(
            "idempotency_keys",
            key=f"report:{confirm_key}", scope="attendance_report",
            response_code=200, response_body=json.dumps(result, ensure_ascii=False),
            created_at=now_ts(),
        )
        self.db.audit(actor, "attendance_report", "attendance_segments", first_segment_id,
                      {"confirm_key": confirm_key, "worker_id": worker_id,
                       "segments": len(segments), "total_minutes": result["total_minutes"]})
        return result

    def _cached_report(self, confirm_key):
        row = self.db.query_one(
            "SELECT response_body FROM idempotency_keys WHERE key=?",
            (f"report:{confirm_key}",),
        )
        return json.loads(row["response_body"]) if row else None

    def _validate_punch_refs(self, worker_id, start_id, end_id, exclude_group=None):
        refs = []
        for punch_id, label in ((start_id, "开始"), (end_id, "结束")):
            if punch_id is None:
                refs.append(None)
                continue
            punch = self.db.get("punch_events", punch_id)
            if punch is None:
                raise not_found(f"{label}打卡事件不存在")
            if punch["worker_id"] != worker_id:
                raise bad_request(f"{label}打卡不属于该人员")
            reused = self.db.query_one(
                """SELECT 1 FROM attribution_links l
                   JOIN attendance_segments s ON s.id=l.segment_id
                   WHERE l.punch_event_id=? AND s.superseded_by IS NULL
                     AND s.report_group IS NOT ? LIMIT 1""",
                (punch_id, exclude_group),
            )
            if reused:
                raise conflict(
                    f"{label}打卡已用于其他有效确认单，再次使用会重复计工",
                    code="punch_already_used",
                )
            refs.append(punch_id)
        return refs

    def _supersede(self, old_group, worker_id, actor):
        old_segments = self.db.query(
            "SELECT * FROM attendance_segments WHERE report_group=? ORDER BY id",
            (old_group,),
        )
        if not old_segments:
            raise not_found("被取代的确认单不存在")
        old_ids = [s["id"] for s in old_segments]
        placeholders = ",".join("?" for _ in old_ids)
        sealed = self.db.query_one(
            f"SELECT 1 FROM pay_items WHERE worker_id=? AND ref_type='segment' "
            f"AND ref_id IN ({placeholders}) AND batch_id IS NOT NULL LIMIT 1",
            [worker_id] + old_ids,
        )
        if sealed:
            raise conflict("相关薪酬已封账，不能重算，请走申诉流程", code="sealed")
        return old_group

    def _mark_superseded(self, old_group, new_group, new_segment_id, worker_id, actor):
        """旧段保留、标记被取代；关联的小时项与计件项置为 reversed（不删）。"""
        old_segments = self.db.query(
            "SELECT id FROM attendance_segments WHERE report_group=?", (old_group,)
        )
        old_ids = [s["id"] for s in old_segments]
        placeholders = ",".join("?" for _ in old_ids)
        self.db.execute(
            f"UPDATE attendance_segments SET superseded_by=? WHERE report_group=?",
            (new_segment_id, old_group),
        )
        self.db.execute(
            f"""UPDATE pay_items SET status='reversed', withheld_reason=
                '确认单 {new_group} 重算取代原确认单 {old_group}'
                WHERE worker_id=? AND status='active' AND (
                  (ref_type='segment' AND ref_id IN ({placeholders}))
                  OR (ref_type='production' AND ref_id IN (
                      SELECT id FROM production_records WHERE segment_id IN ({placeholders})))
                )""",
            [worker_id] + old_ids + old_ids,
        )
        self.db.audit(actor, "supersede_report", "attendance_segments", new_segment_id,
                      {"old_group": old_group, "new_group": new_group})

    def _create_hourly_item(self, worker_id, operation, category, segment_id, seg, actor):
        rate = self._hourly_rate(operation, category)
        amount = amount_for_minutes(seg["minutes"], rate)
        note = {
            "training": "培训等待补贴",
            "standby": "设备停机保底工资",
            "rework": "返工工时",
            "regular": "正常工时",
        }[category]
        item_id = self.db.insert(
            "pay_items",
            worker_id=worker_id,
            work_date=seg["segment_date"],
            category=category,
            ref_type="segment",
            ref_id=segment_id,
            minutes=seg["minutes"],
            quantity=None,
            unit_rate_cents=rate,
            amount_cents=amount,
            note=note + ("（顶班）" if seg["role"] == "substitute" else ""),
            created_at=now_ts(),
        )
        self.db.audit(actor, "create_pay_item", "pay_items", item_id,
                      {"category": category, "amount_cents": amount})
        return self._item_json(dict(self.db.get("pay_items", item_id)))

    def _hourly_rate(self, operation, category):
        setting_key = RATE_SETTINGS[category]
        if setting_key:
            value = self.db.query_one("SELECT value FROM settings WHERE key=?", (setting_key,))
            if value is not None:
                return int(value["value"])
        return operation["hourly_rate_cents"]

    # --- 查询 ---
    def list_segments(self, worker_id=None, day_from=None, day_to=None, include_superseded=False):
        sql = "SELECT * FROM attendance_segments WHERE 1=1"
        params = []
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        if worker_id is not None:
            sql += " AND worker_id=?"
            params.append(worker_id)
        if day_from:
            sql += " AND segment_date>=?"
            params.append(day_from)
        if day_to:
            sql += " AND segment_date<=?"
            params.append(day_to)
        rows = self.db.query(sql + " ORDER BY started_at", params)
        return [self._segment_json(dict(r)) for r in rows]

    def _segment_json(self, seg):
        seg["duration_minutes"] = seg["minutes"]
        return seg

    def _item_json(self, item):
        item["amount_yuan"] = cents_to_yuan(item["amount_cents"])
        item["unit_rate_yuan"] = cents_to_yuan(item["unit_rate_cents"])
        return item
