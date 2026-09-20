"""用工计酬核心命令服务。

职责链：
  资质核验（培训/健康证/可操作工序）→ 班组长编入班次 → 打卡/换岗/顶班/
  中断事件 → 按规则拆分计酬 → 质检与责任复核 → 申诉 → 封账支付批次 → 统计

所有写操作只做两件事：校验 + 向事件日志追加事件。读取时回放日志重算，
因此封账批次可以在任意时刻按封账时点的事件序号重新核算并比对哈希。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from events import EventStore, DuplicateEventError
from rules import (
    SEG_DOWN,
    SEG_PIECE,
    SEG_REGULAR,
    SEG_TRAINING_WAIT,
    SEG_NIGHT,
    Interval,
    PieceRecord,
    Adjustment,
    Rules,
    build_daily_line,
    fmt_ts,
    hours_between,
    normalize_intervals,
    parse_date,
    parse_ts,
    split_at_midnight,
)
from state import replay, replay_at


class DomainError(Exception):
    """业务规则拒绝（400）。"""


class NotFoundError(DomainError):
    """资源不存在（404）。"""


class ConflictError(DomainError):
    """重复提交/幂等冲突（409）。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _build_coverage(snap):
    """按编排计算每个工人的实际工作窗口（换岗/顶班窗口从原岗位者扣除）。

    返回 worker_id -> [{start,end,operation_id,shift_id,work_date,assignment_id}]
    """
    windows: dict[str, list[dict]] = {}
    holes: dict[str, list[dict]] = {}
    for shift in snap.shifts.values():
        work_date = shift["start"][:10]
        for assignment in shift["assignments"].values():
            if assignment.get("parent_assignment_id"):
                windows.setdefault(assignment["worker_id"], []).append(
                    {"start": assignment["window_start"], "end": assignment["window_end"],
                     "operation_id": assignment["operation_id"], "shift_id": shift["id"],
                     "work_date": work_date, "assignment_id": assignment["id"]})
                holes.setdefault(assignment["outgoing_worker_id"], []).append(
                    {"start": assignment["window_start"], "end": assignment["window_end"],
                     "base_assignment_id": assignment["parent_assignment_id"]})
            else:
                windows.setdefault(assignment["worker_id"], []).append(
                    {"start": shift["start"], "end": shift["end"],
                     "operation_id": assignment["operation_id"], "shift_id": shift["id"],
                     "work_date": work_date, "assignment_id": assignment["id"]})
    for worker_id, worker_holes in holes.items():
        clipped: list[dict] = []
        for window in windows.get(worker_id, []):
            segments = [(window["start"], window["end"])]
            for hole in worker_holes:
                if hole["base_assignment_id"] != window["assignment_id"]:
                    continue
                next_segments = []
                for s, e in segments:
                    if hole["end"] <= s or hole["start"] >= e:
                        next_segments.append((s, e))
                    else:
                        if hole["start"] > s:
                            next_segments.append((s, hole["start"]))
                        if hole["end"] < e:
                            next_segments.append((hole["end"], e))
                segments = next_segments
            for s, e in segments:
                if s < e:
                    clipped.append({**window, "start": s, "end": e})
        windows[worker_id] = clipped
    return windows


def _build_spans(snap):
    """原始打卡配对成实际在岗区间；更正区间替换对应原始配对，原始打卡永不删除。

    返回 worker_id -> (spans, unpaired)：
      spans    = [(start, end, [punch_ids])]
      unpaired = [{punch_id, dir, ts, why}]  有上班无下班（或反之）的卡
    """
    spans: dict[str, list] = {}
    unpaired: dict[str, list] = {}
    for worker_id, punches in snap.punches.items():
        corrected_ids = set()
        overrides = []
        for correction in snap.corrections:
            if correction["worker_id"] != worker_id:
                continue
            corrected_ids.update(correction["original_punch_ids"])
            overrides.append(correction)
        pairs = []
        open_punch = None
        worker_unpaired = []
        for punch in sorted(punches, key=lambda p: (p["ts"], p["seq"])):
            if punch["punch_id"] in corrected_ids:
                continue
            if punch["dir"] == "in":
                if open_punch is not None:
                    worker_unpaired.append(
                        {"punch_id": open_punch["punch_id"], "dir": "in",
                         "ts": open_punch["ts"], "why": "连续上班卡，缺少下班卡"})
                open_punch = punch
            else:
                if open_punch is None:
                    worker_unpaired.append(
                        {"punch_id": punch["punch_id"], "dir": "out",
                         "ts": punch["ts"], "why": "下班卡缺少对应上班卡"})
                else:
                    pairs.append((open_punch["ts"], punch["ts"],
                                  [open_punch["punch_id"], punch["punch_id"]]))
                    open_punch = None
        if open_punch is not None:
            worker_unpaired.append(
                {"punch_id": open_punch["punch_id"], "dir": "in",
                 "ts": open_punch["ts"], "why": "缺少下班卡"})
        for correction in overrides:
            pairs.append((correction["correct_start"], correction["correct_end"],
                          correction["original_punch_ids"]))
        spans[worker_id] = sorted(pairs, key=lambda pair: pair[0])
        unpaired[worker_id] = worker_unpaired
    return spans, unpaired


def _coverage_overlaps(snap, worker_id, start, end, exclude_shift=None) -> bool:
    s, e = parse_ts(start), parse_ts(end)
    for window in _build_coverage(snap).get(worker_id, []):
        if exclude_shift and window["shift_id"] == exclude_shift:
            continue
        if s < parse_ts(window["end"]) and parse_ts(window["start"]) < e:
            return True
    return False


def _is_in_shift(snap, shift, worker_id, start, end) -> bool:
    s, e = parse_ts(start), parse_ts(end)
    for window in _build_coverage(snap).get(worker_id, []):
        if window["shift_id"] != shift["id"]:
            continue
        if s >= parse_ts(window["start"]) and e <= parse_ts(window["end"]):
            return True
    return False


def _date_closed(snap, work_date: str) -> bool:
    return any(
        b["status"] == "closed" and b["period_start"] <= work_date <= b["period_end"]
        for b in snap.batches.values()
    )


def _merge(segments: list[Interval]) -> list[Interval]:
    """合并相接/重叠的时间区间（仅用于求并集，kind/meta 取首段）。"""
    ordered = sorted(segments, key=lambda iv: iv.start)
    merged: list[Interval] = []
    for seg in ordered:
        if merged and seg.start <= merged[-1].end:
            if seg.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, seg.end, merged[-1].kind,
                                      merged[-1].hourly_wage, dict(merged[-1].meta))
        else:
            merged.append(Interval(seg.start, seg.end, seg.kind, seg.hourly_wage, dict(seg.meta)))
    return merged


def _clip_overlay(segments: list[Interval], start: str, end: str, kind: str) -> list[Interval]:
    """用 [start,end) 覆盖工作段：相交部分改标为 kind（停机）。"""
    s, e = parse_ts(start), parse_ts(end)
    out: list[Interval] = []
    for seg in segments:
        ss, ee = parse_ts(seg.start), parse_ts(seg.end)
        left, right = max(s, ss), min(e, ee)
        if right <= left:
            out.append(seg)
            continue
        if left > ss:
            out.append(Interval(seg.start, fmt_ts(left), seg.kind, seg.hourly_wage, dict(seg.meta)))
        out.append(Interval(fmt_ts(left), fmt_ts(right), kind, seg.hourly_wage, dict(seg.meta)))
        if right < ee:
            out.append(Interval(fmt_ts(right), seg.end, seg.kind, seg.hourly_wage, dict(seg.meta)))
    out.sort(key=lambda iv: (iv.start, iv.end))
    return out


def _intersect(segments: list[Interval], start: str, end: str,
               kind: str | None = None, meta: dict | None = None) -> list[Interval]:
    """求 segments 与 [start,end) 的交集；kind/meta 给出时覆盖原分段标记。"""
    s, e = parse_ts(start), parse_ts(end)
    out: list[Interval] = []
    for seg in segments:
        ss, ee = parse_ts(seg.start), parse_ts(seg.end)
        left, right = max(s, ss), min(e, ee)
        if right > left:
            wage = seg.hourly_wage if meta is None else meta.get("hourly_wage")
            out.append(Interval(fmt_ts(left), fmt_ts(right),
                                kind if kind is not None else seg.kind,
                                wage,
                                meta if meta is not None else dict(seg.meta)))
    return out


def _subtract(segments: list[Interval], holes: list[Interval]) -> list[Interval]:
    """从 segments 中挖掉 holes 覆盖的时间。"""
    result = list(segments)
    for hole in sorted(holes, key=lambda iv: iv.start):
        remaining: list[Interval] = []
        hs, he = parse_ts(hole.start), parse_ts(hole.end)
        for seg in result:
            ss, ee = parse_ts(seg.start), parse_ts(seg.end)
            if he <= ss or hs >= ee:
                remaining.append(seg)
                continue
            if hs > ss:
                remaining.append(Interval(seg.start, fmt_ts(hs), seg.kind, seg.hourly_wage, dict(seg.meta)))
            if he < ee:
                remaining.append(Interval(fmt_ts(he), seg.end, seg.kind, seg.hourly_wage, dict(seg.meta)))
        result = remaining
    result.sort(key=lambda iv: iv.start)
    return result


class PayService:
    def __init__(self, store: EventStore, rules: Rules | None = None):
        self.store = store
        self.rules = rules or Rules()

    # ============================== 基础档案 ==============================

    def register_worker(self, worker_id: str, name: str, actor: str, **extra) -> dict:
        snap = replay(self.store)
        if worker_id in snap.workers:
            raise ConflictError(f"工人已存在: {worker_id}")
        return self.store.append("worker_registered",
                                {"worker_id": worker_id, "name": name, **extra}, actor).as_dict()

    def deactivate_worker(self, worker_id: str, actor: str) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        return self.store.append("worker_deactivated", {"worker_id": worker_id}, actor).as_dict()

    def record_training(self, worker_id: str, course_id: str, passed_at: str, actor: str,
                        expires_at: str | None = None, course_name: str | None = None) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        parse_ts(passed_at)
        payload = {"worker_id": worker_id, "course_id": course_id,
                   "course_name": course_name or course_id, "passed_at": passed_at,
                   "expires_at": expires_at}
        return self.store.append("training_recorded", payload, actor).as_dict()

    def record_health_cert(self, worker_id: str, expires_at: str, actor: str,
                           issued_at: str | None = None, cert_id: str | None = None) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        parse_date(expires_at)
        payload = {"worker_id": worker_id, "cert_id": cert_id or _new_id("cert"),
                   "issued_at": issued_at, "expires_at": expires_at}
        return self.store.append("health_cert_recorded", payload, actor).as_dict()

    def create_operation(self, operation_id: str, actor: str, name: str | None = None,
                         hourly_wage: float | None = None, piece_unit_pay: float | None = None,
                         required_trainings: list[str] | None = None,
                         seasonal: bool = True) -> dict:
        snap = replay(self.store)
        if operation_id in snap.operations:
            raise ConflictError(f"工序已存在: {operation_id}")
        payload = {"operation_id": operation_id, "name": name or operation_id,
                   "hourly_wage": hourly_wage, "piece_unit_pay": piece_unit_pay,
                   "required_trainings": required_trainings or [], "seasonal": seasonal}
        return self.store.append("operation_created", payload, actor).as_dict()

    # ============================== 资质闸门 ==============================

    def check_eligibility(self, worker_id: str, operation_id: str, at_time: str) -> dict:
        """编入班次前必须通过：食品安全培训、可操作工序、健康证明有效期。"""
        snap = replay(self.store)
        worker = self._require_worker(snap, worker_id)
        operation = snap.operations.get(operation_id)
        if operation is None:
            raise NotFoundError(f"工序不存在: {operation_id}")
        moment = parse_ts(at_time)
        problems: list[str] = []

        if not worker["active"]:
            problems.append("工人已停用")
        for course_id in operation["required_trainings"]:
            record = worker["trainings"].get(course_id)
            if record is None:
                problems.append(f"缺少必需培训: {course_id}")
                continue
            if parse_ts(record["passed_at"]) > moment:
                problems.append(f"培训尚未完成: {course_id}")
            if record.get("expires_at") and parse_date(record["expires_at"]) < moment.date():
                problems.append(f"培训已过期: {course_id}")

        # 健康证明以最新登记的一本为准（换证后旧证不再作为有效凭证）
        valid_cert = None
        if worker["certs"]:
            latest = max(worker["certs"].values(), key=lambda c: c["recorded_seq"])
            if parse_date(latest["expires_at"]) >= moment.date():
                valid_cert = latest
        if not worker["certs"]:
            problems.append("缺少健康证明")
        elif valid_cert is None:
            problems.append("健康证明已过期")

        return {"eligible": not problems, "worker_id": worker_id,
                "operation_id": operation_id, "at": at_time, "problems": problems,
                "valid_health_cert": valid_cert["cert_id"] if valid_cert else None,
                "trainings": list(worker["trainings"].keys())}

    # ============================== 班次与编排 ==============================

    def create_shift(self, shift_id: str, operation_id: str, start: str, end: str,
                     actor: str, leader_id: str | None = None) -> dict:
        snap = replay(self.store)
        if shift_id in snap.shifts:
            raise ConflictError(f"班次已存在: {shift_id}")
        if operation_id not in snap.operations:
            raise NotFoundError(f"工序不存在: {operation_id}")
        start_dt, end_dt = parse_ts(start), parse_ts(end)
        if end_dt <= start_dt:
            raise DomainError("班次结束时间必须晚于开始时间")
        if hours_between(start_dt, end_dt) > self.rules.shift_max_hours:
            raise DomainError(f"单班工时超过上限 {self.rules.shift_max_hours} 小时")
        payload = {"shift_id": shift_id, "operation_id": operation_id,
                   "start": start, "end": end, "leader_id": leader_id}
        return self.store.append("shift_created", payload, actor).as_dict()

    def assign_worker(self, shift_id: str, worker_id: str, actor: str,
                      assignment_id: str | None = None,
                      operation_id: str | None = None) -> dict:
        """班组长把通过资质核验的人员编入班次。"""
        snap = replay(self.store)
        shift = self._require_shift(snap, shift_id)
        operation_id = operation_id or shift["operation_id"]
        result = self.check_eligibility(worker_id, operation_id, shift["start"])
        if not result["eligible"]:
            raise DomainError("资质核验未通过: " + "；".join(result["problems"]))
        if _coverage_overlaps(snap, worker_id, shift["start"], shift["end"],
                              exclude_shift=shift_id):
            raise ConflictError("该工人在此时间段已被编入其他班次")
        payload = {"shift_id": shift_id,
                   "assignment_id": assignment_id or _new_id("asg"),
                   "worker_id": worker_id, "operation_id": operation_id}
        return self.store.append("worker_assigned", payload, actor).as_dict()

    def substitute_worker(self, shift_id: str, base_assignment_id: str,
                          replacement_worker_id: str, start: str, end: str, actor: str,
                          reason: str = "temporary_sub",
                          operation_id: str | None = None) -> dict:
        """换岗/临时顶班：生效窗口内由替岗者计酬，原岗位者该窗口不计工时。"""
        snap = replay(self.store)
        shift = self._require_shift(snap, shift_id)
        base = shift["assignments"].get(base_assignment_id)
        if base is None:
            raise NotFoundError(f"原编排不存在: {base_assignment_id}")
        if reason not in ("temporary_sub", "job_rotation"):
            raise DomainError("原因只能是 temporary_sub（临时顶班）或 job_rotation（换岗）")
        if not (shift["start"] <= start < end <= shift["end"]):
            raise DomainError("顶班窗口必须落在班次时间内")
        for other in shift["assignments"].values():
            if other.get("parent_assignment_id") == base_assignment_id \
                    and start < other["window_end"] and end > other["window_start"]:
                raise ConflictError("该岗位在此窗口已有替岗安排")
        operation_id = operation_id or base["operation_id"]
        result = self.check_eligibility(replacement_worker_id, operation_id, start)
        if not result["eligible"]:
            raise DomainError("替岗者资质核验未通过: " + "；".join(result["problems"]))
        if _coverage_overlaps(snap, replacement_worker_id, start, end,
                              exclude_shift=shift_id):
            raise ConflictError("替岗者在此时间段已有其他工作安排")
        payload = {"shift_id": shift_id, "assignment_id": _new_id("asg"),
                   "base_assignment_id": base_assignment_id,
                   "outgoing_worker_id": base["worker_id"],
                   "replacement_worker_id": replacement_worker_id,
                   "operation_id": operation_id, "start": start, "end": end,
                   "reason": reason}
        return self.store.append("assignment_replaced", payload, actor).as_dict()

    # ============================== 打卡（原始记录不可变） ==============================

    def record_punch(self, worker_id: str, direction: str, ts: str, actor: str,
                     scanner_id: str | None = None, source: str = "scanner",
                     client_event_id: str | None = None,
                     punch_id: str | None = None) -> dict:
        """扫码器打卡。离线补传或班组长重复确认不得增加工时：

        - 同一 client_event_id 由事件存储幂等拒绝；
        - 同人同方向同时间的打卡按内容去重拒绝。
        """
        if direction not in ("in", "out"):
            raise DomainError("打卡方向只能是 in/out")
        if source not in ("scanner", "offline_upload", "leader_confirm"):
            raise DomainError("打卡来源只能是 scanner/offline_upload/leader_confirm")
        parse_ts(ts)
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        for existing in snap.punches.get(worker_id, []):
            if existing["dir"] == direction and existing["ts"] == ts:
                raise ConflictError(
                    f"重复打卡已拒绝（{worker_id} {direction} {ts}，原始序号 {existing['seq']}），工时不重复计算")
        payload = {"punch_id": punch_id or _new_id("punch"), "worker_id": worker_id,
                   "dir": direction, "ts": ts, "scanner_id": scanner_id,
                   "source": source, "client_event_id": client_event_id}
        try:
            return self.store.append("punch_recorded", payload, actor).as_dict()
        except DuplicateEventError as error:
            raise ConflictError(
                f"离线补传/重复确认已拒绝（原始序号 {error.original_seq}），工时不重复计算"
            ) from None

    def correct_punches(self, worker_id: str, original_punch_ids: list[str],
                        correct_start: str, correct_end: str, actor: str,
                        reason: str) -> dict:
        """打卡更正：原始打卡保留并标记，仅追加更正，按更正后区间计酬。"""
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        if not reason:
            raise DomainError("更正必须说明原因")
        start_dt, end_dt = parse_ts(correct_start), parse_ts(correct_end)
        if end_dt <= start_dt:
            raise DomainError("更正后的结束时间必须晚于开始时间")
        known = {p["punch_id"] for p in snap.punches.get(worker_id, [])}
        missing = [pid for pid in original_punch_ids if pid not in known]
        if missing:
            raise NotFoundError(f"原始打卡不存在: {missing}")
        work_date = correct_start[:10]
        if _date_closed(snap, work_date):
            raise DomainError("该日计酬已封账，更正只能走责任复核/申诉调整，不得悄然修改")
        payload = {"worker_id": worker_id, "original_punch_ids": original_punch_ids,
                   "correct_start": correct_start, "correct_end": correct_end,
                   "reason": reason}
        return self.store.append("punch_corrected", payload, actor).as_dict()

    # ============================== 中断事件 ==============================

    def record_incident(self, shift_id: str, worker_id: str, kind: str,
                        start: str, end: str, actor: str, note: str = "",
                        incident_id: str | None = None) -> dict:
        """设备停机保底 / 无薪休息中断 / 培训等待。时间必须在该工人本班次窗口内。"""
        if kind not in ("equipment_down", "break", "training_wait"):
            raise DomainError("中断类型只能是 equipment_down/break/training_wait")
        snap = replay(self.store)
        shift = self._require_shift(snap, shift_id)
        self._require_worker(snap, worker_id)
        if not (start < end):
            raise DomainError("中断开始时间必须早于结束时间")
        if kind == "training_wait":
            # 培训等待发生在开班前到岗等待：与班次同日、结束不晚于班次结束
            if start[:10] != shift["start"][:10] or end > shift["end"]:
                raise DomainError("培训等待必须发生在开班当日且不晚于班次结束")
        elif not (shift["start"] <= start and end <= shift["end"]):
            raise DomainError("中断时间必须落在班次时间内")
        if kind in ("equipment_down", "break") and \
                not _is_in_shift(snap, shift, worker_id, start, end):
            raise DomainError("该工人在此窗口不属于此班次（请先编入或办理顶班）")
        for other in snap.incidents:
            if other["worker_id"] != worker_id or other["kind"] != kind:
                continue
            if start < other["end"] and end > other["start"]:
                raise ConflictError("与已登记的同类中断时间重叠")
        payload = {"incident_id": incident_id or _new_id("inc"), "shift_id": shift_id,
                   "worker_id": worker_id, "kind": kind, "start": start, "end": end,
                   "note": note}
        return self.store.append("incident_recorded", payload, actor).as_dict()

    # ============================== 计件产量与质检责任复核 ==============================

    def report_quantity(self, worker_id: str, operation_id: str, shift_id: str,
                        quantity: int, actor: str, unit_pay: float | None = None,
                        quantity_id: str | None = None) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        operation = snap.operations.get(operation_id)
        if operation is None:
            raise NotFoundError(f"工序不存在: {operation_id}")
        shift = self._require_shift(snap, shift_id)
        if quantity <= 0:
            raise DomainError("产量必须为正数")
        if _date_closed(snap, shift["start"][:10]):
            raise DomainError("该班次所在日期已封账，补报产量请走申诉/调整流程留痕")
        pay = unit_pay if unit_pay is not None else operation.get("piece_unit_pay")
        if pay is None:
            raise DomainError("该工序未设置单件工价，请显式提供 unit_pay")
        payload = {"quantity_id": quantity_id or _new_id("qty"), "worker_id": worker_id,
                   "operation_id": operation_id, "shift_id": shift_id,
                   "quantity": quantity, "unit_pay": pay}
        return self.store.append("quantity_reported", payload, actor).as_dict()

    def qc_decide(self, quantity_id: str, result: str, actor: str,
                  evidence: str = "") -> dict:
        snap = replay(self.store)
        item = snap.quantities.get(quantity_id)
        if item is None:
            raise NotFoundError(f"产量记录不存在: {quantity_id}")
        if result not in ("accepted", "rejected"):
            raise DomainError("质检结论只能是 accepted/rejected")
        if result == "rejected" and not evidence:
            raise DomainError("退回必须附带质检证据（照片/单号/说明）")
        if item["status"] != "pending":
            raise ConflictError(f"该产量已有质检结论: {item['status']}")
        return self.store.append("quantity_qc_decided",
                                 {"quantity_id": quantity_id, "result": result,
                                  "evidence": evidence}, actor).as_dict()

    def open_review(self, quantity_id: str, actor: str, evidence: str | None = None,
                    review_id: str | None = None) -> dict:
        """退回产量进入有证据的责任复核，而不是直接扣个人报酬。"""
        snap = replay(self.store)
        item = snap.quantities.get(quantity_id)
        if item is None:
            raise NotFoundError(f"产量记录不存在: {quantity_id}")
        if item["status"] != "rejected_pending_review":
            raise ConflictError("只有被质检退回的产量才能开启责任复核")
        payload = {"review_id": review_id or _new_id("rev"), "quantity_id": quantity_id,
                   "evidence": evidence if evidence is not None else item["evidence"]}
        return self.store.append("review_opened", payload, actor).as_dict()

    def conclude_review(self, review_id: str, responsibility: str, actor: str,
                        accepted_qty: int | None = None, note: str = "",
                        evidence: str = "") -> dict:
        snap = replay(self.store)
        review = snap.reviews.get(review_id)
        if review is None:
            raise NotFoundError(f"复核不存在: {review_id}")
        if review["status"] != "open":
            raise ConflictError("复核已结论")
        if responsibility not in ("worker", "non_worker", "shared"):
            raise DomainError("责任结论只能是 worker/non_worker/shared")
        if not note or not evidence:
            raise DomainError("复核结论必须给出说明与证据")
        item = snap.quantities.get(review["quantity_id"])
        if accepted_qty is None:
            accepted_qty = 0 if responsibility == "worker" else item["quantity"]
        if not 0 <= accepted_qty <= item["quantity"]:
            raise DomainError("合格件数超出产量范围")

        # 若退回件已随封账批次按原额暂计，确认为个人责任的差额不回改旧批次，
        # 而是生成一条带证据的负向调整项，进入下一批次——旧批次保持不可变。
        post_close_adjustment = None
        if item.get("paid") and accepted_qty < item["quantity"] \
                and responsibility in ("worker", "shared"):
            delta = -round((item["quantity"] - accepted_qty) * item["unit_pay"], 2)
            adj_payload = {"adjustment_id": _new_id("adj"), "worker_id": item["worker_id"],
                           "kind": "rework_deduct", "amount": delta,
                           "reason": f"封账后责任复核确认个人责任，产量 {item['quantity_id']} "
                                     f"合格 {accepted_qty}/{item['quantity']}",
                           "evidence": evidence,
                           "work_date": snap.shifts[item["shift_id"]]["start"][:10]}
            self.store.append("adjustment_recorded", adj_payload, actor)
            post_close_adjustment = adj_payload["adjustment_id"]

        payload = {"review_id": review_id, "responsibility": responsibility,
                   "accepted_qty": accepted_qty, "note": note, "evidence": evidence,
                   "post_close_adjustment": post_close_adjustment}
        return self.store.append("review_concluded", payload, actor).as_dict()

    # ============================== 申诉 ==============================

    def open_appeal(self, worker_id: str, reason: str, actor: str,
                    scope_type: str = "payroll", scope_id: str | None = None,
                    appeal_id: str | None = None) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        if scope_type not in ("payroll", "review", "punch"):
            raise DomainError("申诉范围只能是 payroll/review/punch")
        if not reason:
            raise DomainError("申诉必须填写事由")
        payload = {"appeal_id": appeal_id or _new_id("apl"), "worker_id": worker_id,
                   "scope_type": scope_type, "scope_id": scope_id, "reason": reason}
        return self.store.append("appeal_opened", payload, actor).as_dict()

    def resolve_appeal(self, appeal_id: str, decision: str, actor: str,
                       note: str = "", grant_amount: float | None = None,
                       grant_reason: str | None = None,
                       work_date: str | None = None) -> dict:
        snap = replay(self.store)
        appeal = snap.appeals.get(appeal_id)
        if appeal is None:
            raise NotFoundError(f"申诉不存在: {appeal_id}")
        if appeal["status"] != "open":
            raise ConflictError("申诉已处理")
        if decision not in ("granted", "denied", "partial"):
            raise DomainError("决定只能是 granted/denied/partial")
        if decision in ("granted", "partial"):
            if grant_amount is None or grant_amount <= 0:
                raise DomainError("申诉成立必须给出补发金额")
            if not note:
                raise DomainError("申诉成立必须给出处理说明（作为补发证据）")
            if work_date is None:
                raise DomainError("补发必须注明归属计酬日期")
            parse_date(work_date)
        adjustment_id = None
        if decision in ("granted", "partial"):
            adjustment_id = _new_id("adj")
            self.store.append("adjustment_recorded",
                              {"adjustment_id": adjustment_id,
                               "worker_id": appeal["worker_id"], "kind": "appeal_grant",
                               "amount": round(grant_amount, 2),
                               "reason": grant_reason or f"申诉 {appeal_id} 成立补发",
                               "evidence": note, "appeal_id": appeal_id,
                               "work_date": work_date}, actor)
        payload = {"appeal_id": appeal_id, "decision": decision, "note": note,
                   "adjustment_id": adjustment_id}
        return self.store.append("appeal_resolved", payload, actor).as_dict()

    def record_adjustment(self, worker_id: str, kind: str, amount: float, reason: str,
                          work_date: str, actor: str, evidence: str = "",
                          adjustment_id: str | None = None) -> dict:
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        if kind not in ("rework_deduct", "appeal_grant", "other"):
            raise DomainError("调整类型只能是 rework_deduct/appeal_grant/other")
        if not reason:
            raise DomainError("调整项必须说明原因")
        if kind == "rework_deduct" and amount >= 0:
            raise DomainError("责任扣减金额应为负数")
        if kind == "rework_deduct" and not evidence:
            raise DomainError("责任扣减必须附带复核证据")
        parse_date(work_date)
        payload = {"adjustment_id": adjustment_id or _new_id("adj"),
                   "worker_id": worker_id, "kind": kind, "amount": round(amount, 2),
                   "reason": reason, "evidence": evidence, "work_date": work_date}
        return self.store.append("adjustment_recorded", payload, actor).as_dict()

    # ============================== 计酬重算 ==============================

    def worker_payroll(self, worker_id: str, period_start: str, period_end: str) -> dict:
        """村民查看自己的逐项计算、原始打卡与申诉进展。"""
        snap = replay(self.store)
        self._require_worker(snap, worker_id)
        result = self._compute(snap, period_start, period_end, only_worker=worker_id)
        result["total_hours"] = round(
            sum(w["total_hours"] for w in result["workers"]), 2)
        result["raw_punches"] = [
            {k: p[k] for k in
             ("punch_id", "dir", "ts", "source", "scanner_id", "seq", "corrected_by")}
            for p in sorted(snap.punches.get(worker_id, []), key=lambda p: p["seq"])
            if period_start <= p["ts"][:10] <= period_end
        ]
        result["corrections"] = [c for c in snap.corrections if c["worker_id"] == worker_id]
        result["appeals"] = [a for a in snap.appeals.values() if a["worker_id"] == worker_id]
        result["reviews"] = [
            r for r in snap.reviews.values()
            if snap.quantities.get(r["quantity_id"], {}).get("worker_id") == worker_id
        ]
        result["unattested_incidents"] = [
            item for item in result["unattested_incidents"]
            if item["worker_id"] == worker_id
        ]
        result["paid_batches"] = [
            {"batch_id": b["batch_id"], "name": b["name"],
             "period_start": b["period_start"], "period_end": b["period_end"],
             "total_amount": b["total_amount"], "closed_at": b["closed_at"]}
            for b in snap.batches.values()
            if worker_id in b["worker_ids"]
        ]
        # 已封账批次中的逐项明细同样向本人公开（冻结值，不可变）
        result["closed_lines"] = [
            line for b in snap.batches.values()
            if worker_id in b["worker_ids"]
            for line in b["lines"]
            if line["worker_id"] == worker_id
            and period_start <= line["work_date"] <= period_end
        ]
        result["closed_hours"] = round(
            sum(sum(line["hours_by_kind"].values()) for line in result["closed_lines"]), 2)
        result["closed_amount"] = round(
            sum(line["gross_amount"] for line in result["closed_lines"]), 2)
        return result

    def compute_payroll(self, period_start: str, period_end: str) -> dict:
        return self._compute(replay(self.store), period_start, period_end)

    def _gather_intervals(self, snap, only_worker=None):
        """汇总每工人的计酬区间：

        在岗工时 = 编排窗口 ∩ 打卡区间（打卡是在岗证据）；
        设备停机 = 停机窗口 ∩ 在岗工时，按保底计；
        培训等待 = 等待窗口 ∩ 打卡区间 − 编排窗口（已排岗部分按正常工时）；
        无打卡佐证的等待/停机不计酬，仅返回告警，交班组长核对。

        跨午夜的所有分段都携带班次起始日（meta.work_date），随班次日封账。
        返回 intervals: worker -> [Interval]，unattested: [告警]。
        """
        windows = _build_coverage(snap)
        spans, unpaired = _build_spans(snap)
        incidents: dict[str, list[dict]] = {}
        for inc in snap.incidents:
            incidents.setdefault(inc["worker_id"], []).append(inc)

        intervals: dict[str, list[Interval]] = {}
        unattested: list[dict] = []
        worker_ids = set(windows) | set(spans) | set(incidents)
        if only_worker:
            worker_ids = {wid for wid in worker_ids if wid == only_worker}

        for worker_id in worker_ids:
            merged_spans = _merge([Interval(s, e) for s, e, _ids in spans.get(worker_id, [])])
            merged_windows = _merge(
                [Interval(w["start"], w["end"], meta={**w}) for w in windows.get(worker_id, [])]
            )
            bucket: list[Interval] = []

            # 1) 在岗工时：编排窗口 ∩ 打卡
            for window in merged_windows:
                op = snap.operations.get(window.meta["operation_id"], {})
                is_piece = op.get("piece_unit_pay") is not None
                kind = SEG_PIECE if is_piece else SEG_REGULAR
                meta = {"assignment_id": window.meta.get("assignment_id"),
                        "shift_id": window.meta.get("shift_id"),
                        "operation_id": window.meta.get("operation_id"),
                        "work_date": window.meta.get("work_date"),
                        "hourly_wage": op.get("hourly_wage"),
                        "piece": is_piece}
                bucket.extend(
                    _intersect(merged_spans, window.start, window.end, kind=kind, meta=meta)
                )

            # 2) 设备停机：只对有打卡佐证的在岗工时保底
            for inc in incidents.get(worker_id, []):
                if inc["kind"] != "equipment_down":
                    continue
                punched = _intersect(merged_spans, inc["start"], inc["end"])
                if not punched:
                    unattested.append({"worker_id": worker_id, "incident_id": inc["incident_id"],
                                       "kind": "equipment_down", "start": inc["start"],
                                       "end": inc["end"],
                                       "reason": "停机时段无打卡记录，不计保底，请核对"})
                bucket = _clip_overlay(bucket, inc["start"], inc["end"], SEG_DOWN)

            # 3) 培训等待：有打卡、未排岗、非停机的部分
            attendance_union = _merge(bucket)
            for inc in incidents.get(worker_id, []):
                if inc["kind"] != "training_wait":
                    continue
                punched = _intersect(merged_spans, inc["start"], inc["end"])
                if not punched:
                    unattested.append({"worker_id": worker_id, "incident_id": inc["incident_id"],
                                       "kind": "training_wait", "start": inc["start"],
                                       "end": inc["end"],
                                       "reason": "等待时段无打卡记录，不计等待报酬，请核对"})
                    continue
                wait = _subtract(punched, attendance_union)
                shift_day = snap.shifts[inc["shift_id"]]["start"][:10]
                for iv in wait:
                    iv.kind = SEG_TRAINING_WAIT
                    iv.meta = {"shift_id": inc["shift_id"], "work_date": shift_day, "piece": False,
                               "operation_id": None, "incident_id": inc["incident_id"]}
                bucket.extend(wait)

            if bucket:
                intervals[worker_id] = normalize_intervals(bucket, self.rules)

            # 4) 打卡完整性：有上班无下班等未配对卡
            for item in unpaired.get(worker_id, []):
                unattested.append({"worker_id": worker_id, "incident_id": None,
                                   "kind": "unpaired_punch", "punch_id": item["punch_id"],
                                   "start": item["ts"], "end": item["ts"],
                                   "reason": item["why"] + "，该卡不计工时，请补卡更正"})

            # 5) 有打卡但完全未排岗的时段：不静默计酬，也不静默丢弃。
            # 已登记的培训等待时段除外（那本就是"未排岗但计等待薪"的情形）。
            wait_cover = [
                Interval(inc["start"], inc["end"])
                for inc in incidents.get(worker_id, [])
                if inc["kind"] == "training_wait"
            ]
            not_scheduled = _subtract(_subtract(merged_spans, merged_windows), wait_cover)
            for iv in not_scheduled:
                unattested.append({"worker_id": worker_id, "incident_id": None,
                                   "kind": "unscheduled_punch", "start": iv.start,
                                   "end": iv.end,
                                   "reason": "打卡时段未编入任何班次，不计工时，请班组长核对"})

        return intervals, unattested

    def _compute(self, snap, period_start: str, period_end: str, only_worker: str | None = None) -> dict:
        parse_date(period_start)
        parse_date(period_end)
        if period_start > period_end:
            raise DomainError("计酬周期起始晚于结束")

        raw_intervals, unattested = self._gather_intervals(snap, only_worker)

        # 跨午夜拆分 + 夜班标注；分段归属班次起始日（meta.work_date），随班次日封账
        intervals_by_day: dict[str, dict[str, list[Interval]]] = {}
        excluded_paid_attendance: list[dict] = []
        for worker_id, items in raw_intervals.items():
            normalized = normalize_intervals(items, self.rules)
            for iv in normalized:
                day = iv.meta.get("work_date") or iv.start[:10]
                if (worker_id, day) in snap.paid_worker_dates:
                    if period_start <= day <= period_end:
                        # 连续封账下不应出现；出现说明周期与已封账日期重叠
                        excluded_paid_attendance.append(
                            {"worker_id": worker_id, "work_date": day,
                             "start": iv.start, "end": iv.end,
                             "note": "该日已封账，此段不再计入新批次，如有异议请走申诉"})
                    # 早于本周期的历史出勤直接忽略，不重复计酬
                    continue
                intervals_by_day.setdefault(worker_id, {}).setdefault(day, []).append(iv)

        breaks_by_day: dict[str, dict[str, list[Interval]]] = {}
        for inc in snap.incidents:
            if inc["kind"] != "break":
                continue
            worker_id = inc["worker_id"]
            if only_worker and worker_id != only_worker:
                continue
            shift_day = snap.shifts[inc["shift_id"]]["start"][:10]
            if (worker_id, shift_day) in snap.paid_worker_dates:
                continue
            for piece in split_at_midnight(Interval(inc["start"], inc["end"])):
                breaks_by_day.setdefault(worker_id, {}).setdefault(shift_day, []).append(piece)

        # 未付计件按班次起始日归集；封账后补报为迟交项（写操作已阻断，这里双保险）
        pieces_by_day: dict[str, dict[str, list[PieceRecord]]] = {}
        late_quantities: list[dict] = []
        provisional_items: list[dict] = []
        pending_qc: list[dict] = []
        for q in snap.quantities.values():
            if only_worker and q["worker_id"] != only_worker:
                continue
            if q["paid"]:
                continue
            day = snap.shifts[q["shift_id"]]["start"][:10]
            if (q["worker_id"], day) in snap.paid_worker_dates:
                late_quantities.append({"worker_id": q["worker_id"],
                                        "quantity_id": q["quantity_id"], "work_date": day})
                continue
            record = PieceRecord(id=q["quantity_id"], operation_id=q["operation_id"],
                                 quantity=q["quantity"], unit_pay=q["unit_pay"],
                                 status=q["status"], evidence=q["evidence"],
                                 accepted_qty=q.get("accepted_qty", 0))
            pieces_by_day.setdefault(q["worker_id"], {}).setdefault(day, []).append(record)
            if q["status"] == "pending":
                pending_qc.append(
                    {"worker_id": q["worker_id"], "quantity_id": q["quantity_id"],
                     "work_date": day, "quantity": q["quantity"],
                     "note": "尚未质检，计件额挂账，不计入本批次"})
            if q["status"] == "rejected_pending_review":
                provisional_items.append(
                    {"worker_id": q["worker_id"], "quantity_id": q["quantity_id"],
                     "work_date": day, "amount_provisional": record.payable_amount(),
                     "note": "责任复核未结论，按原额暂计"})

        # 未付调整项（含封账后复核扣减/申诉补发，凭证据进入下一批次，不回改旧批次）
        adjustments_by_day: dict[str, dict[str, list[Adjustment]]] = {}
        for adj in snap.adjustments.values():
            if adj["paid"]:
                continue
            if only_worker and adj["worker_id"] != only_worker:
                continue
            if adj["work_date"] > period_end:
                continue
            adjustments_by_day.setdefault(adj["worker_id"], {}).setdefault(
                adj["work_date"], []).append(
                Adjustment(adj["adjustment_id"], adj["kind"], adj["amount"],
                           adj["reason"], adj["evidence"], adj.get("appeal_id", "")))

        worker_ids = set(intervals_by_day) | set(pieces_by_day) | set(adjustments_by_day)
        if only_worker:
            worker_ids.add(only_worker)

        workers_out: list[dict] = []
        for worker_id in sorted(worker_ids):
            days = (set(intervals_by_day.get(worker_id, {}))
                    | set(pieces_by_day.get(worker_id, {}))
                    | set(adjustments_by_day.get(worker_id, {})))
            lines: list[dict] = []
            for day in sorted(days):
                if day > period_end:
                    continue
                intervals = intervals_by_day.get(worker_id, {}).get(day, [])
                pieces = pieces_by_day.get(worker_id, {}).get(day, [])
                adjustments = adjustments_by_day.get(worker_id, {}).get(day, [])
                # 早于周期起始的未付调整项是封账后复核/申诉的产物，随本批支付；
                # 更早日期的工时/产量已被连续封账覆盖，不会出现在这里。
                if day < period_start and not adjustments:
                    continue
                breaks = breaks_by_day.get(worker_id, {}).get(day, [])
                line = build_daily_line(worker_id, day, intervals, breaks, pieces,
                                        adjustments, self.rules)
                lines.append(line.as_dict())
            if not lines:
                continue
            total_hours = round(
                sum(sum(line["hours_by_kind"].values()) for line in lines), 2)
            total_amount = round(sum(line["gross_amount"] for line in lines), 2)
            workers_out.append({"worker_id": worker_id,
                                "name": snap.workers.get(worker_id, {}).get("name", ""),
                                "lines": lines, "total_hours": total_hours,
                                "total_amount": total_amount})

        return {"period_start": period_start, "period_end": period_end,
                "workers": workers_out, "worker_count": len(workers_out),
                "total_amount": round(sum(w["total_amount"] for w in workers_out), 2),
                "provisional_items": provisional_items,
                "pending_qc": pending_qc,
                "late_quantities": late_quantities,
                "excluded_paid_attendance": excluded_paid_attendance,
                "unattested_incidents": unattested}

    # ============================== 封账支付批次 ==============================

    def close_payroll(self, name: str, period_start: str, period_end: str, actor: str,
                      allow_provisional: bool = False) -> dict:
        snap = replay(self.store)
        closed = [b for b in snap.batches.values() if b["status"] == "closed"]
        if closed:
            latest_end = max(b["period_end"] for b in closed)
            expected_start = (parse_date(latest_end) + timedelta(days=1)).strftime("%Y-%m-%d")
            if period_start != expected_start:
                raise DomainError(
                    f"新批次必须从 {expected_start} 起连续封账（上一批止于 {latest_end}），不得漏封/重封")
        computed = self._compute(snap, period_start, period_end)
        if computed["late_quantities"]:
            raise DomainError("存在已封账日期的补报产量，请先通过申诉/调整流程处理")
        if computed["excluded_paid_attendance"]:
            raise DomainError("存在落在已封账日期的打卡，请先通过申诉/调整流程处理")
        if computed["worker_count"] == 0:
            raise DomainError("周期内没有可封账的计酬内容")
        unpaired = [i for i in computed["unattested_incidents"]
                    if i["kind"] == "unpaired_punch"
                    and period_start <= i["start"][:10] <= period_end]
        if unpaired:
            raise DomainError(
                f"存在 {len(unpaired)} 条缺少上班/下班配对的打卡，请补卡更正后再封账")
        if computed["pending_qc"]:
            raise DomainError(
                "存在尚未质检的产量（计件额挂账，工时保底仍计），完成质检后再封账；"
                "计件差额将随后续批次支付")
        if computed["provisional_items"] and not allow_provisional:
            raise DomainError(
                "存在责任复核未结论的退回产量（按原额暂计），请先完成复核或显式 allow_provisional")
        batch_id = _new_id("pay")
        lines = [line for w in computed["workers"] for line in w["lines"]]
        worker_ids = sorted({w["worker_id"] for w in computed["workers"]})
        summary = {"batch_id": batch_id, "period_start": period_start,
                   "period_end": period_end, "worker_ids": worker_ids,
                   "lines": lines, "total_amount": computed["total_amount"]}
        payload = {**summary, "name": name,
                   "summary_hash": self._summary_hash(summary),
                   "event_seq_at_close": len(self.store.all()),
                   "provisional_items": computed["provisional_items"],
                   "unattested_incidents": computed["unattested_incidents"]}
        self.store.append("payroll_closed", payload, actor)
        return self.get_batch(batch_id)

    def list_batches(self) -> list[dict]:
        snap = replay(self.store)
        return [{"batch_id": b["batch_id"], "name": b["name"],
                 "period_start": b["period_start"], "period_end": b["period_end"],
                 "worker_count": len(b["worker_ids"]),
                 "total_amount": b["total_amount"], "closed_at": b["closed_at"],
                 "summary_hash": b.get("summary_hash")}
                for b in sorted(snap.batches.values(), key=lambda x: x["closed_seq"])]

    def get_batch(self, batch_id: str) -> dict:
        snap = replay(self.store)
        batch = snap.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"支付批次不存在: {batch_id}")
        return {"batch_id": batch["batch_id"], "name": batch["name"],
                "period_start": batch["period_start"], "period_end": batch["period_end"],
                "worker_ids": batch["worker_ids"], "lines": batch["lines"],
                "total_amount": batch["total_amount"],
                "summary_hash": batch.get("summary_hash"),
                "closed_at": batch["closed_at"],
                "closed_seq": batch["closed_seq"],
                "event_seq_at_close": batch.get("event_seq_at_close"),
                "provisional_items": batch.get("provisional_items", []),
                "unattested_incidents": batch.get("unattested_incidents", [])}

    def verify_batch(self, batch_id: str) -> dict:
        """按封账时点事件序号重放重算，证明批次内容没有被悄然修改。"""
        snap_now = replay(self.store)
        batch = snap_now.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"支付批次不存在: {batch_id}")
        chain = self.store.verify()
        # 重放到封账事件的前一条：批次是在那一刻的状态上计算并冻结的
        snap_then = replay_at(self.store, batch["closed_seq"] - 1)
        recomputed = self._compute(snap_then, batch["period_start"], batch["period_end"])
        recomputed_lines = [line for w in recomputed["workers"] for line in w["lines"]]
        same_lines = json.dumps(recomputed_lines, ensure_ascii=False, sort_keys=True) == \
            json.dumps(batch["lines"], ensure_ascii=False, sort_keys=True)
        summary = {"batch_id": batch_id, "period_start": batch["period_start"],
                   "period_end": batch["period_end"], "worker_ids": batch["worker_ids"],
                   "lines": batch["lines"], "total_amount": batch["total_amount"]}
        same_hash = self._summary_hash(summary) == batch.get("summary_hash")

        piece_ids = {row["piece_id"] for line in batch["lines"] for row in line.get("pieces", [])}
        quantity_to_worker = {
            qid: snap_now.quantities[qid]["worker_id"]
            for qid in piece_ids if qid in snap_now.quantities
        }
        related_events = []
        for event in self.store.since(batch["closed_seq"]):
            p = event.payload
            touched = False
            if p.get("worker_id") in batch["worker_ids"]:
                touched = True
            elif p.get("quantity_id") in piece_ids:
                touched = True
            elif event.type == "review_concluded" \
                    and quantity_to_worker.get(
                        snap_now.reviews.get(p.get("review_id"), {}).get("quantity_id")):
                touched = True
            elif event.type == "adjustment_recorded" \
                    and any(line["work_date"] == p.get("work_date")
                            and line["worker_id"] == p.get("worker_id")
                            for line in batch["lines"]):
                touched = True
            if touched:
                related_events.append({"seq": event.seq, "ts": event.ts,
                                       "type": event.type, "actor": event.actor,
                                       "payload": p})
        return {"batch_id": batch_id, "chain_ok": chain["ok"],
                "stored_hash_matches": same_hash,
                "recomputed_matches": same_lines,
                "ok": chain["ok"] and same_hash and same_lines,
                "closed_seq": batch["closed_seq"],
                "post_close_related_events": related_events}

    # ============================== 季节性岗位统计 ==============================

    def seasonal_stats(self, period_start: str, period_end: str) -> dict:
        """管理者核对：季节性岗位实际带动就业人数、有效工时与已付金额。

        按编排岗位的 seasonal 标记过滤；同一工人当天既干节令岗又干常年岗时，
        只统计节令岗位的分段工时与对应金额。
        """
        snap = replay(self.store)
        parse_date(period_start)
        parse_date(period_end)

        def is_seasonal_op(op_id: str) -> bool:
            return snap.operations.get(op_id, {}).get("seasonal", True)

        workers: set[str] = set()
        person_days: set[tuple[str, str]] = set()
        shift_count = 0
        for shift in snap.shifts.values():
            day = shift["start"][:10]
            if not (period_start <= day <= period_end):
                continue
            op = snap.operations.get(shift["operation_id"], {})
            if not op.get("seasonal", True):
                continue
            ids = {a["worker_id"] for a in shift["assignments"].values()}
            if ids:
                shift_count += 1
            workers.update(ids)
            person_days.update((wid, day) for wid in ids)

        computed = self._compute(snap, period_start, period_end)
        hours_by_kind: dict = {}
        effective_hours = 0.0
        seasonal_computed = 0.0

        def line_is_seasonal(line) -> bool:
            ops = {s.get("operation_id") for s in line["segments"]} | \
                  {p.get("operation_id") for p in line["pieces"]}
            return all(is_seasonal_op(op_id) for op_id in ops if op_id)

        def add_line_hours(line):
            nonlocal effective_hours
            for seg in line["segments"]:
                if not is_seasonal_op(seg.get("operation_id")):
                    continue
                hours_by_kind[seg["kind"]] = round(
                    hours_by_kind.get(seg["kind"], 0) + seg["hours"], 2)
                effective_hours = round(effective_hours + seg["hours"], 2)

        def seasonal_line_amount(line):
            # 全日节令岗位（含纯调整项行）直接取 gross，其已含复核扣减/申诉补发
            if not line["segments"] and not line["pieces"]:
                return round(line["gross_amount"], 2)
            if line["pieces"] and line["guaranteed_amount"] and line_is_seasonal(line):
                return round(line["gross_amount"], 2)
            seg_amt = sum(s["amount"] for s in line["segments"]
                          if is_seasonal_op(s.get("operation_id")))
            piece_amt = sum(p["amount"] for p in line["pieces"]
                            if is_seasonal_op(p.get("operation_id")))
            adj_amt = sum(a["amount"] for a in line["adjustments"])
            return round(seg_amt + piece_amt + adj_amt, 2)

        for worker in computed["workers"]:
            for line in worker["lines"]:
                add_line_hours(line)
                seasonal_computed = round(seasonal_computed + seasonal_line_amount(line), 2)

        paid_amount = 0.0
        paid_batches = []
        for batch in snap.batches.values():
            if batch["status"] != "closed":
                continue
            if batch["period_end"] < period_start or batch["period_start"] > period_end:
                continue
            batch_seasonal = 0.0
            for line in batch["lines"]:
                if not (period_start <= line["work_date"] <= period_end):
                    continue
                add_line_hours(line)
                batch_seasonal = round(batch_seasonal + seasonal_line_amount(line), 2)
            paid_amount = round(paid_amount + batch_seasonal, 2)
            paid_batches.append(batch["batch_id"])

        return {"period_start": period_start, "period_end": period_end,
                "seasonal_employment_count": len(workers),
                "person_days": len(person_days), "shift_count": shift_count,
                "effective_hours": effective_hours, "hours_by_kind": hours_by_kind,
                "computed_amount_unclosed": seasonal_computed,
                "paid_amount": paid_amount, "paid_batches": paid_batches}

    def list_workers(self) -> list[dict]:
        snap = replay(self.store)
        return [{"worker_id": w["worker_id"], "name": w["name"], "active": w["active"],
                 "trainings": list(w["trainings"].values()),
                 "certs": list(w["certs"].values())}
                for w in snap.workers.values()]

    def list_operations(self) -> list[dict]:
        snap = replay(self.store)
        return list(snap.operations.values())

    def list_shifts(self) -> list[dict]:
        snap = replay(self.store)
        return [{"shift_id": s["id"], "operation_id": s["operation_id"],
                 "start": s["start"], "end": s["end"], "leader_id": s.get("leader_id"),
                 "assignments": list(s["assignments"].values())}
                for s in sorted(snap.shifts.values(), key=lambda x: x["created_seq"])]

    def list_quantities(self, worker_id: str | None = None) -> list[dict]:
        snap = replay(self.store)
        return [q for q in snap.quantities.values()
                if worker_id is None or q["worker_id"] == worker_id]

    def list_reviews(self) -> list[dict]:
        return list(replay(self.store).reviews.values())

    def list_appeals(self, worker_id: str | None = None) -> list[dict]:
        return [a for a in replay(self.store).appeals.values()
                if worker_id is None or a["worker_id"] == worker_id]

    def verify_chain(self, disk: bool = False) -> dict:
        if disk and self.store.path:
            result = EventStore.verify_disk(self.store.path)
            result["source"] = "disk"
            result["path"] = self.store.path
            return result
        result = self.store.verify()
        result["source"] = "memory"
        return result

    @staticmethod
    def _summary_hash(summary: dict) -> str:
        canon = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_worker(snap, worker_id: str) -> dict:
        worker = snap.workers.get(worker_id)
        if worker is None:
            raise NotFoundError(f"工人不存在: {worker_id}")
        return worker

    @staticmethod
    def _require_shift(snap, shift_id: str) -> dict:
        shift = snap.shifts.get(shift_id)
        if shift is None:
            raise NotFoundError(f"班次不存在: {shift_id}")
        return shift
