"""村民申诉与处理进展。

申诉提交后进入 submitted，人事/生产可受理（reviewing）、解决（resolved）
或驳回（rejected）。每次状态流转都写入不可变事件流，村民可查看完整进展。
"""

from .errors import bad_request, conflict, forbidden, not_found
from .timeutil import now_ts

VALID_CATEGORIES = ("hours", "quality", "pay", "other")
STAFF_ROLES = ("hr", "production", "leader", "finance", "admin")


class Appeals:
    def __init__(self, db):
        self.db = db

    def submit(self, worker_id, data, actor):
        worker = self.db.get("workers", worker_id)
        if worker is None:
            raise not_found("人员不存在")
        category = data.get("category", "other")
        if category not in VALID_CATEGORIES:
            raise bad_request(f"category 必须是 {VALID_CATEGORIES} 之一")
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise bad_request("申诉事由必填")
        pay_item_id = data.get("pay_item_id")
        if pay_item_id is not None:
            item = self.db.get("pay_items", pay_item_id)
            if item is None or item["worker_id"] != worker_id:
                raise bad_request("关联薪酬项不存在或不属于本人")
        now = now_ts()
        appeal_id = self.db.insert(
            "appeals",
            worker_id=worker_id,
            category=category,
            pay_item_id=pay_item_id,
            reason=reason,
            evidence=data.get("evidence"),
            status="submitted",
            created_at=now,
            updated_at=now,
        )
        self._event(appeal_id, "submit", reason, actor)
        self.db.audit(actor, "submit_appeal", "appeals", appeal_id,
                      {"worker_id": worker_id, "category": category})
        return self.get_appeal(appeal_id)

    def transition(self, appeal_id, action, message, actor, actor_role):
        if actor_role not in STAFF_ROLES:
            raise forbidden("只有工作人员可以处理申诉")
        appeal = self.db.get("appeals", appeal_id)
        if appeal is None:
            raise not_found("申诉不存在")
        transitions = {
            "accept": ("submitted", "reviewing", "accept"),
            "resolve": ("reviewing", "resolved", "resolve"),
            "reject": ("reviewing", "rejected", "reject"),
        }
        if action not in transitions:
            raise bad_request("action 必须是 accept、resolve 或 reject")
        required_from, to, event_type = transitions[action]
        if appeal["status"] != required_from:
            raise conflict(
                f"申诉当前状态为 {appeal['status']}，不能执行 {action}（需 {required_from}）",
                code="invalid_transition",
            )
        message = (message or "").strip()
        if action in ("resolve", "reject") and not message:
            raise bad_request("处理结论必须填写说明")
        self.db.execute(
            "UPDATE appeals SET status=?, updated_at=? WHERE id=?",
            (to, now_ts(), appeal_id),
        )
        self._event(appeal_id, event_type, message or "已受理", actor)
        self.db.audit(actor, f"appeal_{action}", "appeals", appeal_id,
                      {"to": to})
        return self.get_appeal(appeal_id)

    def add_note(self, appeal_id, message, actor, worker_id=None):
        appeal = self.db.get("appeals", appeal_id)
        if appeal is None:
            raise not_found("申诉不存在")
        if worker_id is not None and appeal["worker_id"] != worker_id:
            raise forbidden("只能查看本人申诉")
        message = (message or "").strip()
        if not message:
            raise bad_request("说明内容必填")
        self._event(appeal_id, "note", message, actor)
        self.db.execute("UPDATE appeals SET updated_at=? WHERE id=?", (now_ts(), appeal_id))
        return self.get_appeal(appeal_id)

    def list_for_worker(self, worker_id):
        rows = self.db.query(
            "SELECT * FROM appeals WHERE worker_id=? ORDER BY id DESC", (worker_id,))
        return [self.get_appeal(r["id"]) for r in rows]

    def list_all(self, status=None):
        if status:
            rows = self.db.query("SELECT * FROM appeals WHERE status=? ORDER BY id DESC",
                                 (status,))
        else:
            rows = self.db.query("SELECT * FROM appeals ORDER BY id DESC")
        return [self.get_appeal(r["id"]) for r in rows]

    def get_appeal(self, appeal_id):
        row = self.db.get("appeals", appeal_id)
        if row is None:
            raise not_found("申诉不存在")
        result = dict(row)
        result["events"] = [
            dict(e) for e in self.db.query(
                "SELECT * FROM appeal_events WHERE appeal_id=? ORDER BY id", (appeal_id,))
        ]
        return result

    def _event(self, appeal_id, event_type, message, actor):
        self.db.insert(
            "appeal_events",
            appeal_id=appeal_id,
            event_type=event_type,
            message=message,
            actor=actor,
            created_at=now_ts(),
        )
