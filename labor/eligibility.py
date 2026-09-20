"""上岗资质核验：食品安全培训、可操作工序、健康证明。"""

import json

from .timeutil import now_ts, parse_day


def check_eligibility(db, worker_id, operation_id, work_day):
    """返回 (ok, snapshot, reasons)。

    work_day 为班次开始日（YYYY-MM-DD）。跨午夜的班次仍以开工日核验，
    编入时的完整结果作为快照留存。
    """
    day = parse_day(work_day)
    worker = db.get("workers", worker_id)
    operation = db.get("operations", operation_id)
    reasons = []
    snapshot = {
        "checked_at": now_ts(),
        "work_date": day,
        "worker_id": worker_id,
        "operation_id": operation_id,
    }
    if worker is None:
        reasons.append("人员不存在")
    elif worker["terminated_at"]:
        reasons.append("人员已离场，不能编入班次")
    if operation is None:
        reasons.append("工序不存在")

    if operation is not None and operation["requires_food_safety"]:
        training = db.query_one(
            """SELECT * FROM food_safety_trainings
               WHERE worker_id=? AND valid_until>=?
               ORDER BY valid_until DESC LIMIT 1""",
            (worker_id, day),
        )
        if training is None:
            reasons.append("食品安全培训缺失或已过期")
            snapshot["food_safety"] = {"valid": False}
        else:
            snapshot["food_safety"] = {
                "valid": True,
                "training_id": training["id"],
                "trained_at": training["trained_at"],
                "valid_until": training["valid_until"],
            }

    cert = db.query_one(
        """SELECT * FROM health_certificates
           WHERE worker_id=? AND valid_until>=?
           ORDER BY valid_until DESC LIMIT 1""",
        (worker_id, day),
    )
    if cert is None:
        reasons.append("健康证明缺失或已过期")
        snapshot["health_certificate"] = {"valid": False}
    else:
        snapshot["health_certificate"] = {
            "valid": True,
            "certificate_id": cert["id"],
            "certificate_no": cert["certificate_no"],
            "valid_until": cert["valid_until"],
        }

    if operation is not None:
        grant = db.query_one(
            "SELECT * FROM worker_operations WHERE worker_id=? AND operation_id=?",
            (worker_id, operation_id),
        )
        if grant is None:
            reasons.append(f"未取得工序「{operation['name']}」的操作授权")
            snapshot["operation_authorization"] = {"valid": False}
        else:
            snapshot["operation_authorization"] = {
                "valid": True,
                "grant_id": grant["id"],
                "granted_at": grant["granted_at"],
            }

    snapshot["passed"] = not reasons
    snapshot["reasons"] = reasons
    return (not reasons), snapshot, reasons


def snapshot_text(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
