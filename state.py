"""事件回放得到的只读状态（读模型）。

每次命令从事件日志重放得到快照，避免双写造成的不一致。
状态层不做业务裁决，只负责索引；业务规则在 commands.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from events import EventStore


@dataclass
class Snapshot:
    workers: dict = field(default_factory=dict)          # worker_id -> 工人档案
    operations: dict = field(default_factory=dict)       # operation_id -> 工序
    shifts: dict = field(default_factory=dict)           # shift_id -> 班次（含 assignments）
    punches: dict = field(default_factory=dict)          # worker_id -> [原始打卡]
    corrections: list = field(default_factory=list)      # 打卡更正
    incidents: list = field(default_factory=list)        # 停机/休息/培训等待
    quantities: dict = field(default_factory=dict)       # quantity_id -> 产量
    reviews: dict = field(default_factory=dict)          # review_id -> 责任复核
    appeals: dict = field(default_factory=dict)          # appeal_id -> 申诉
    adjustments: dict = field(default_factory=dict)      # adjustment_id -> 调整项
    batches: dict = field(default_factory=dict)          # batch_id -> 支付批次
    client_event_ids: dict = field(default_factory=dict)  # 幂等去重索引
    paid_worker_dates: set = field(default_factory=set)   # 已封账 (worker_id, work_date)
    last_seq: int = 0

    # ---- 查询辅助 ----

    def assignments_for_worker(self, worker_id: str) -> list[dict]:
        result = []
        for shift in self.shifts.values():
            for assignment in shift["assignments"].values():
                if assignment["worker_id"] == worker_id:
                    result.append({**assignment, "shift_id": shift["id"], "shift": shift})
        return result

    def paid_periods(self, worker_id: str) -> list[tuple[str, str, str]]:
        """该工人已封账的区间：(起, 止, batch_id)。"""
        result = []
        for batch in self.batches.values():
            if batch["status"] == "closed" and worker_id in batch["worker_ids"]:
                result.append((batch["period_start"], batch["period_end"], batch["id"]))
        return result


def replay(store: EventStore) -> Snapshot:
    snap = Snapshot()
    for event in store.all():
        _apply(snap, event)
        snap.last_seq = event.seq
    return snap


def replay_at(store: EventStore, seq: int) -> Snapshot:
    """重放至第 seq 条事件（含），用于复核封账时刻的快照。"""
    snap = Snapshot()
    for event in store.all():
        if event.seq > seq:
            break
        _apply(snap, event)
        snap.last_seq = event.seq
    return snap


def _apply(snap: Snapshot, event) -> None:
    p = event.payload
    t = event.type

    if t == "worker_registered":
        snap.workers[p["worker_id"]] = {
            "worker_id": p["worker_id"],
            "name": p.get("name", ""),
            "id_card_hint": p.get("id_card_hint", ""),
            "phone": p.get("phone", ""),
            "active": True,
            "trainings": {},   # course_id -> 记录
            "certs": {},       # cert_id -> 记录
            "registered_at": event.ts,
        }
    elif t == "worker_deactivated":
        if p["worker_id"] in snap.workers:
            snap.workers[p["worker_id"]]["active"] = False

    elif t == "training_recorded":
        worker = snap.workers.get(p["worker_id"])
        if worker is not None:
            worker["trainings"][p["course_id"]] = {
                "course_id": p["course_id"],
                "course_name": p.get("course_name", p["course_id"]),
                "passed_at": p["passed_at"],
                "expires_at": p.get("expires_at"),
                "recorded_seq": event.seq,
            }

    elif t == "health_cert_recorded":
        worker = snap.workers.get(p["worker_id"])
        if worker is not None:
            worker["certs"][p["cert_id"]] = {
                "cert_id": p["cert_id"],
                "issued_at": p.get("issued_at"),
                "expires_at": p["expires_at"],
                "recorded_seq": event.seq,
            }

    elif t == "operation_created":
        snap.operations[p["operation_id"]] = {
            "operation_id": p["operation_id"],
            "name": p.get("name", p["operation_id"]),
            "hourly_wage": p.get("hourly_wage"),
            "piece_unit_pay": p.get("piece_unit_pay"),
            "seasonal": p.get("seasonal", True),
            "required_trainings": list(p.get("required_trainings", [])),
        }

    elif t == "shift_created":
        snap.shifts[p["shift_id"]] = {
            "id": p["shift_id"],
            "operation_id": p["operation_id"],
            "start": p["start"],
            "end": p["end"],
            "leader_id": p.get("leader_id"),
            "assignments": {},
            "created_seq": event.seq,
        }

    elif t == "worker_assigned":
        shift = snap.shifts.get(p["shift_id"])
        if shift is not None:
            shift["assignments"][p["assignment_id"]] = {
                "id": p["assignment_id"],
                "worker_id": p["worker_id"],
                "operation_id": p.get("operation_id", shift["operation_id"]),
                "parent_assignment_id": None,
                "kind": "regular",
                "seq": event.seq,
            }

    elif t == "assignment_replaced":
        # 临时顶班/换岗：在原编排上叠加一个生效窗口，窗口内由替岗者计酬
        shift = snap.shifts.get(p["shift_id"])
        if shift is not None:
            shift["assignments"][p["assignment_id"]] = {
                "id": p["assignment_id"],
                "worker_id": p["replacement_worker_id"],
                "operation_id": p.get("operation_id", shift["operation_id"]),
                "parent_assignment_id": p["base_assignment_id"],
                "kind": p.get("reason", "temporary_sub"),
                "window_start": p["start"],
                "window_end": p["end"],
                "outgoing_worker_id": p.get("outgoing_worker_id"),
                "seq": event.seq,
            }

    elif t == "punch_recorded":
        cid = p.get("client_event_id")
        if cid is not None:
            snap.client_event_ids.setdefault(cid, event.seq)
        snap.punches.setdefault(p["worker_id"], []).append(
            {
                "punch_id": p["punch_id"],
                "dir": p["dir"],
                "ts": p["ts"],
                "scanner_id": p.get("scanner_id"),
                "source": p.get("source", "scanner"),
                "client_event_id": cid,
                "seq": event.seq,
                "corrected_by": None,
            }
        )

    elif t == "punch_corrected":
        snap.corrections.append(
            {
                "worker_id": p["worker_id"],
                "original_punch_ids": list(p.get("original_punch_ids", [])),
                "correct_start": p["correct_start"],
                "correct_end": p["correct_end"],
                "reason": p.get("reason", ""),
                "seq": event.seq,
            }
        )
        for punch in snap.punches.get(p["worker_id"], []):
            if punch["punch_id"] in p.get("original_punch_ids", []):
                punch["corrected_by"] = event.seq

    elif t == "incident_recorded":
        snap.incidents.append(
            {
                "incident_id": p["incident_id"],
                "shift_id": p["shift_id"],
                "worker_id": p["worker_id"],
                "start": p["start"],
                "end": p["end"],
                "kind": p["kind"],
                "note": p.get("note", ""),
                "seq": event.seq,
            }
        )

    elif t == "quantity_reported":
        snap.quantities[p["quantity_id"]] = {
            "quantity_id": p["quantity_id"],
            "worker_id": p["worker_id"],
            "operation_id": p["operation_id"],
            "shift_id": p.get("shift_id"),
            "quantity": p["quantity"],
            "unit_pay": p["unit_pay"],
            "status": "pending",
            "evidence": "",
            "accepted_qty": 0,
            "review_id": None,
            "reported_seq": event.seq,
            "paid": False,
            "paid_batch_id": None,
        }

    elif t == "quantity_qc_decided":
        item = snap.quantities.get(p["quantity_id"])
        if item is not None:
            item["qc_decision_seq"] = event.seq
            if p["result"] == "accepted":
                item["status"] = "accepted"
                item["accepted_qty"] = item["quantity"]
            else:
                item["status"] = "rejected_pending_review"
                item["evidence"] = p.get("evidence", "")

    elif t == "review_opened":
        item = snap.quantities.get(p["quantity_id"])
        if item is not None:
            item["status"] = "rejected_pending_review"
            item["review_id"] = p["review_id"]
            item["evidence"] = p.get("evidence", item["evidence"])
        snap.reviews[p["review_id"]] = {
            "review_id": p["review_id"],
            "quantity_id": p["quantity_id"],
            "evidence": p.get("evidence", ""),
            "status": "open",
            "conclusion": None,
            "opened_seq": event.seq,
        }

    elif t == "review_concluded":
        review = snap.reviews.get(p["review_id"])
        item = snap.quantities.get(review["quantity_id"]) if review else None
        if review is not None:
            review["status"] = "concluded"
            review["conclusion"] = p["responsibility"]
            review["accepted_qty"] = p.get("accepted_qty", 0)
            review["note"] = p.get("note", "")
            review["evidence"] = p.get("evidence", review["evidence"])
            review["concluded_seq"] = event.seq
            review["post_close_adjustment"] = p.get("post_close_adjustment")
        if item is not None:
            if p["responsibility"] == "worker":
                item["status"] = "rejected_worker_fault"
            elif p["responsibility"] == "non_worker":
                item["status"] = "rejected_non_worker"
            else:
                item["status"] = "rejected_worker_fault"  # shared 按部分个人责任计
                item["shared"] = True
            item["accepted_qty"] = p.get("accepted_qty", 0)
            if p.get("evidence"):
                item["evidence"] = p["evidence"]

    elif t == "appeal_opened":
        snap.appeals[p["appeal_id"]] = {
            "appeal_id": p["appeal_id"],
            "worker_id": p["worker_id"],
            "scope_type": p.get("scope_type"),
            "scope_id": p.get("scope_id"),
            "reason": p.get("reason", ""),
            "status": "open",
            "decision": None,
            "resolution_note": "",
            "adjustment_id": None,
            "opened_seq": event.seq,
            "events": [{"seq": event.seq, "status": "open", "at": event.ts}],
        }

    elif t == "appeal_resolved":
        appeal = snap.appeals.get(p["appeal_id"])
        if appeal is not None:
            appeal["status"] = "resolved"
            appeal["decision"] = p["decision"]
            appeal["resolution_note"] = p.get("note", "")
            appeal["adjustment_id"] = p.get("adjustment_id")
            appeal["events"].append(
                {"seq": event.seq, "status": f"resolved:{p['decision']}", "at": event.ts}
            )

    elif t == "adjustment_recorded":
        snap.adjustments[p["adjustment_id"]] = {
            "adjustment_id": p["adjustment_id"],
            "worker_id": p["worker_id"],
            "kind": p["kind"],
            "amount": p["amount"],
            "reason": p.get("reason", ""),
            "evidence": p.get("evidence", ""),
            "appeal_id": p.get("appeal_id"),
            "work_date": p.get("work_date"),
            "paid": False,
            "paid_batch_id": None,
            "seq": event.seq,
        }

    elif t == "payroll_closed":
        for line in p["lines"]:
            snap.paid_worker_dates.add((line["worker_id"], line["work_date"]))
            for piece in line.get("pieces", []):
                item = snap.quantities.get(piece["piece_id"])
                if item is not None:
                    item["paid"] = True
                    item["paid_batch_id"] = p["batch_id"]
            for adj in line.get("adjustments", []):
                adjustment = snap.adjustments.get(adj["adjustment_id"])
                if adjustment is not None:
                    adjustment["paid"] = True
                    adjustment["paid_batch_id"] = p["batch_id"]
        snap.batches[p["batch_id"]] = {
            "batch_id": p["batch_id"],
            "name": p.get("name", ""),
            "period_start": p["period_start"],
            "period_end": p["period_end"],
            "worker_ids": list(p["worker_ids"]),
            "lines": p["lines"],
            "total_amount": p["total_amount"],
            "summary_hash": p.get("summary_hash"),
            "status": "closed",
            "closed_at": event.ts,
            "closed_seq": event.seq,
            "unattested_incidents": p.get("unattested_incidents", []),
        }
