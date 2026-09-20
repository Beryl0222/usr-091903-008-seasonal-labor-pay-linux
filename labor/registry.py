"""人员、资质、工序与访问令牌登记。"""

import secrets

from .errors import bad_request, conflict, not_found
from .timeutil import now_ts, parse_day, parse_ts, yuan_to_cents


def _require(db, table, row_id, label):
    row = db.get(table, row_id)
    if row is None:
        raise not_found(f"{label}不存在")
    return row


class Registry:
    def __init__(self, db):
        self.db = db

    # --- 人员 ---
    def create_worker(self, data, actor):
        name = (data.get("name") or "").strip()
        if not name:
            raise bad_request("姓名必填")
        worker_id = self.db.insert(
            "workers",
            name=name,
            id_card=data.get("id_card"),
            phone=data.get("phone"),
            created_at=now_ts(),
        )
        token = None
        if data.get("issue_token", True):
            token = self.issue_token("worker", f"worker:{worker_id}", worker_id)
        self.db.audit(actor, "create_worker", "workers", worker_id, {"name": name})
        return {"id": worker_id, "name": name, "phone": data.get("phone"), "token": token}

    def terminate_worker(self, worker_id, actor):
        _require(self.db, "workers", worker_id, "人员")
        self.db.execute(
            "UPDATE workers SET terminated_at=? WHERE id=? AND terminated_at IS NULL",
            (now_ts(), worker_id),
        )
        self.db.audit(actor, "terminate_worker", "workers", worker_id)
        return {"id": worker_id, "terminated": True}

    def list_workers(self):
        rows = self.db.query("SELECT * FROM workers ORDER BY id")
        return [dict(r) for r in rows]

    # --- 食品安全培训 ---
    def add_training(self, worker_id, data, actor):
        _require(self.db, "workers", worker_id, "人员")
        trained_at = fmt_or_date(data.get("trained_at"))
        valid_until = parse_day(data.get("valid_until"))
        row_id = self.db.insert(
            "food_safety_trainings",
            worker_id=worker_id,
            trained_at=trained_at,
            valid_until=valid_until,
            evidence=data.get("evidence"),
        )
        self.db.audit(actor, "add_training", "food_safety_trainings", row_id,
                      {"worker_id": worker_id, "valid_until": valid_until})
        return {"id": row_id, "worker_id": worker_id, "trained_at": trained_at,
                "valid_until": valid_until}

    # --- 健康证明 ---
    def add_health_certificate(self, worker_id, data, actor):
        _require(self.db, "workers", worker_id, "人员")
        issued_at = fmt_or_date(data.get("issued_at"))
        valid_until = parse_day(data.get("valid_until"))
        row_id = self.db.insert(
            "health_certificates",
            worker_id=worker_id,
            issued_at=issued_at,
            valid_until=valid_until,
            certificate_no=data.get("certificate_no"),
            evidence=data.get("evidence"),
        )
        self.db.audit(actor, "add_health_certificate", "health_certificates", row_id,
                      {"worker_id": worker_id, "valid_until": valid_until})
        return {"id": row_id, "worker_id": worker_id, "certificate_no": data.get("certificate_no"),
                "issued_at": issued_at, "valid_until": valid_until}

    # --- 工序与授权 ---
    def create_operation(self, data, actor):
        code = (data.get("code") or "").strip()
        name = (data.get("name") or "").strip()
        if not code or not name:
            raise bad_request("工序编码与名称必填")
        if self.db.query_one("SELECT 1 FROM operations WHERE code=?", (code,)):
            raise conflict("工序编码已存在")
        row_id = self.db.insert(
            "operations",
            code=code,
            name=name,
            hourly_rate_cents=yuan_to_cents(data.get("hourly_rate_yuan", 0)),
            piece_rate_cents=yuan_to_cents(data.get("piece_rate_yuan", 0)),
            requires_food_safety=1 if data.get("requires_food_safety", True) else 0,
        )
        self.db.audit(actor, "create_operation", "operations", row_id, {"code": code})
        return self.get_operation(row_id)

    def get_operation(self, operation_id):
        row = _require(self.db, "operations", operation_id, "工序")
        return dict(row)

    def list_operations(self):
        return [dict(r) for r in self.db.query("SELECT * FROM operations ORDER BY id")]

    def grant_operation(self, worker_id, operation_id, actor):
        _require(self.db, "workers", worker_id, "人员")
        _require(self.db, "operations", operation_id, "工序")
        try:
            row_id = self.db.insert(
                "worker_operations",
                worker_id=worker_id,
                operation_id=operation_id,
                granted_at=now_ts(),
                granted_by=actor,
            )
        except Exception as exc:  # UNIQUE 冲突
            raise conflict("该工序授权已存在") from exc
        self.db.audit(actor, "grant_operation", "worker_operations", row_id,
                      {"worker_id": worker_id, "operation_id": operation_id})
        return {"id": row_id, "worker_id": worker_id, "operation_id": operation_id}

    # --- 令牌 ---
    def issue_token(self, role, subject, worker_id=None):
        token = secrets.token_urlsafe(24)
        self.db.insert("access_tokens", token=token, subject=subject,
                       role=role, worker_id=worker_id)
        return token

    def bootstrap_staff_tokens(self):
        """开发/部署初始化：为五个内置岗位生成令牌（已存在则跳过）。"""
        result = {}
        for role in ("admin", "hr", "production", "leader", "finance"):
            subject = role
            row = self.db.query_one(
                "SELECT token FROM access_tokens WHERE subject=? AND role=?", (subject, role)
            )
            if row is None:
                token = self.issue_token(role, subject)
                result[role] = token
            else:
                result[role] = row["token"]
        return result

    def authenticate(self, token):
        if not token:
            return None
        row = self.db.query_one("SELECT * FROM access_tokens WHERE token=?", (token,))
        return dict(row) if row else None

    # --- 参数 ---
    def set_setting(self, key, value, actor):
        now = now_ts()
        self.db.execute(
            """INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
            (key, str(value), now, actor),
        )
        return {"key": key, "value": str(value)}

    def get_setting(self, key, default=None):
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default


def fmt_or_date(value):
    if value is None:
        return now_ts()
    try:
        return parse_day(value) if len(value) == 10 else parse_ts(value).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError) as exc:
        raise bad_request(f"无法识别的日期：{value}") from exc
