"""班次排班；编入班次前强制三项资质核验。"""

from .eligibility import check_eligibility, snapshot_text
from .errors import bad_request, conflict, not_found, unprocessable
from .timeutil import day_of, fmt, now_ts, parse_day, parse_ts


class Scheduling:
    def __init__(self, db):
        self.db = db

    def create_shift(self, data, actor):
        operation = self.db.get("operations", data.get("operation_id"))
        if operation is None:
            raise not_found("工序不存在")
        start = parse_ts(data.get("planned_start"))
        end = parse_ts(data.get("planned_end"))
        if end <= start:
            raise bad_request("班次结束时间必须晚于开始时间")
        shift_id = self.db.insert(
            "shifts",
            operation_id=operation["id"],
            work_date=day_of(start),
            planned_start=fmt(start),
            planned_end=fmt(end),
            created_at=now_ts(),
            created_by=actor,
        )
        self.db.audit(actor, "create_shift", "shifts", shift_id,
                      {"operation_id": operation["id"], "date": day_of(start)})
        return self.get_shift(shift_id)

    def get_shift(self, shift_id):
        row = self.db.get("shifts", shift_id)
        if row is None:
            raise not_found("班次不存在")
        result = dict(row)
        result["assignments"] = [
            dict(a) for a in self.db.query(
                """SELECT a.*, w.name AS worker_name FROM shift_assignments a
                   JOIN workers w ON w.id=a.worker_id WHERE shift_id=? ORDER BY a.id""",
                (shift_id,),
            )
        ]
        return result

    def list_shifts(self, work_date=None):
        if work_date:
            parse_day(work_date)
            rows = self.db.query("SELECT * FROM shifts WHERE work_date=? ORDER BY planned_start",
                                 (work_date,))
        else:
            rows = self.db.query("SELECT * FROM shifts ORDER BY planned_start DESC")
        return [dict(r) for r in rows]

    def assign_worker(self, shift_id, data, actor):
        """把人员编入班次。先验培训、工序授权、健康证明，缺一不可。"""
        shift = self.db.get("shifts", shift_id)
        if shift is None:
            raise not_found("班次不存在")
        worker_id = data.get("worker_id")
        if not isinstance(worker_id, int):
            raise bad_request("worker_id 必填")
        if shift["locked"]:
            raise conflict("该班次已随支付批次封账，不能再调整人员", code="shift_locked")

        ok, snapshot, reasons = check_eligibility(
            self.db, worker_id, shift["operation_id"], shift["work_date"]
        )
        if not ok:
            self.db.audit(actor, "assign_blocked", "shift_assignments", None,
                          {"shift_id": shift_id, "worker_id": worker_id, "reasons": reasons})
            raise unprocessable(
                "资质核验未通过，不能编入班次：" + "；".join(reasons),
                code="eligibility_failed",
                details={"reasons": reasons, "snapshot": snapshot},
            )

        replaced = data.get("replaced_worker_id")
        role = data.get("role", "normal")
        if role not in ("normal", "substitute"):
            raise bad_request("role 只能是 normal 或 substitute")
        if role == "substitute" and replaced is None:
            raise bad_request("顶班必须指定 replaced_worker_id")
        try:
            assignment_id = self.db.insert(
                "shift_assignments",
                shift_id=shift_id,
                worker_id=worker_id,
                role=role,
                replaced_worker_id=replaced,
                assigned_at=now_ts(),
                assigned_by=actor,
                eligibility_snapshot=snapshot_text(snapshot),
            )
        except Exception as exc:
            raise conflict("该人员已在此班次中") from exc
        self.db.audit(actor, "assign_worker", "shift_assignments", assignment_id,
                      {"shift_id": shift_id, "worker_id": worker_id, "role": role})
        return {"id": assignment_id, "shift_id": shift_id, "worker_id": worker_id,
                "role": role, "replaced_worker_id": replaced,
                "eligibility_snapshot": snapshot}

    def check_worker(self, worker_id, operation_id=None, work_day=None):
        worker = self.db.get("workers", worker_id)
        if worker is None:
            raise not_found("人员不存在")
        op_ids = [r["operation_id"] for r in self.db.query(
            "SELECT operation_id FROM worker_operations WHERE worker_id=?", (worker_id,))]
        if operation_id is not None:
            op_ids = [operation_id]
        result = []
        for op_id in op_ids:
            day = work_day or day_of(parse_ts(now_ts()))
            ok, snapshot, reasons = check_eligibility(self.db, worker_id, op_id, day)
            result.append({"operation_id": op_id, "passed": ok,
                           "reasons": reasons, "snapshot": snapshot})
        return {"worker_id": worker_id, "checks": result}
