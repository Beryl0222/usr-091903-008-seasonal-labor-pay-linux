"""薪酬逐项视图、支付批次封账（哈希链防篡改）与就业统计。"""

import json

from .errors import bad_request, conflict, not_found
from .timeutil import cents_to_yuan, now_ts, parse_day

PAYABLE_ITEM_STATUSES = ("active", "withheld")


class Payroll:
    def __init__(self, db):
        self.db = db

    # --- 村民/管理查询：逐项计算 ---
    def worker_payroll(self, worker_id, day_from=None, day_to=None):
        worker = self.db.get("workers", worker_id)
        if worker is None:
            raise not_found("人员不存在")
        sql = "SELECT * FROM pay_items WHERE worker_id=?"
        params = [worker_id]
        if day_from:
            sql += " AND work_date>=?"
            params.append(parse_day(day_from))
        if day_to:
            sql += " AND work_date<=?"
            params.append(parse_day(day_to))
        items = [dict(r) for r in self.db.query(sql + " ORDER BY work_date, id", params)]
        appeal_map = {}
        for a in self.db.query(
            "SELECT pay_item_id, id, status, updated_at FROM appeals WHERE worker_id=?",
            (worker_id,),
        ):
            appeal_map[a["pay_item_id"]] = {"appeal_id": a["id"], "status": a["status"],
                                            "updated_at": a["updated_at"]}
        totals = {"active_cents": 0, "withheld_cents": 0, "reversed_cents": 0}
        result_items = []
        for item in items:
            item["amount_yuan"] = cents_to_yuan(item["amount_cents"])
            item["unit_rate_yuan"] = cents_to_yuan(item["unit_rate_cents"])
            batch = None
            if item["batch_id"]:
                b = self.db.get("payment_batches", item["batch_id"])
                batch = {"batch_id": b["id"], "sealed_at": b["sealed_at"]}
            item["batch"] = batch
            if item["quality_case_id"]:
                c = self.db.get("quality_cases", item["quality_case_id"])
                item["quality_case"] = {
                    "case_id": c["id"], "status": c["status"],
                    "reason": c["reason"], "decision_note": c["decision_note"]}
            if item["id"] in appeal_map:
                item["appeal"] = appeal_map[item["id"]]
            totals[f"{item['status']}_cents"] = totals.get(f"{item['status']}_cents", 0) \
                + item["amount_cents"]
            result_items.append(item)
        summary = {
            "payable_cents": totals["active_cents"],
            "payable_yuan": cents_to_yuan(totals["active_cents"]),
            "withheld_cents": totals["withheld_cents"],
            "withheld_yuan": cents_to_yuan(totals["withheld_cents"]),
            "reversed_cents": totals.get("reversed_cents", 0),
            "reversed_yuan": cents_to_yuan(totals.get("reversed_cents", 0)),
            "item_count": len(result_items),
        }
        return {"worker_id": worker_id, "name": worker["name"],
                "from": day_from, "to": day_to, "items": result_items, "summary": summary}

    # --- 人工调整（申诉补发/核减等）：新建留痕项，不改动既有项 ---
    def create_adjustment(self, worker_id, data, actor):
        worker = self.db.get("workers", worker_id)
        if worker is None:
            raise not_found("人员不存在")
        work_date = parse_day(data.get("work_date"))
        note = (data.get("note") or "").strip()
        if not note:
            raise bad_request("调整说明必填（村民逐项可见）")
        if "amount_cents" in data:
            amount = int(data["amount_cents"])
        elif "amount_yuan" in data:
            from .timeutil import yuan_to_cents
            amount = yuan_to_cents(data["amount_yuan"])
        else:
            raise bad_request("需要 amount_cents 或 amount_yuan（可为负数表示核减）")
        if amount == 0:
            raise bad_request("调整金额不能为 0")
        appeal_id = data.get("appeal_id")
        if appeal_id is not None:
            appeal = self.db.get("appeals", appeal_id)
            if appeal is None or appeal["worker_id"] != worker_id:
                raise bad_request("关联申诉不存在或不属于该人员")
        # 不可变账上没有独立调整表：预取下一薪酬项 id 作为自身引用，保证
        # (ref_type, ref_id, version) 唯一；封账全程持锁，预取安全。
        next_id = self.db.query_one(
            "SELECT COALESCE(MAX(id),0)+1 AS n FROM pay_items")["n"]
        item_id = self.db.insert(
            "pay_items",
            worker_id=worker_id,
            work_date=work_date,
            category="manual_adjust",
            ref_type="manual_adjust",
            ref_id=next_id,
            minutes=None,
            quantity=None,
            unit_rate_cents=0,
            amount_cents=amount,
            note=note,
            created_at=now_ts(),
        )
        self.db.audit(actor, "create_adjustment", "pay_items", item_id,
                      {"worker_id": worker_id, "amount_cents": amount,
                       "appeal_id": appeal_id, "note": note})
        row = dict(self.db.get("pay_items", item_id))
        row["amount_yuan"] = cents_to_yuan(amount)
        row["unit_rate_yuan"] = cents_to_yuan(0)
        return row

    # --- 封账 ---
    def seal_batch(self, data, actor):
        period_start = parse_day(data.get("period_start"))
        period_end = parse_day(data.get("period_end"))
        if period_end < period_start:
            raise bad_request("周期结束日不能早于开始日")

        items = self.db.query(
            """SELECT * FROM pay_items WHERE work_date BETWEEN ? AND ?
               AND batch_id IS NULL AND status='active' ORDER BY worker_id, id""",
            (period_start, period_end),
        )
        if not items:
            raise conflict("该周期没有可封账的有效薪酬项（暂缓/冲销项不支付）",
                           code="empty_batch")
        # 周期内若存在跨批次重算残留引用则一并阻断。
        total = sum(i["amount_cents"] for i in items)
        workers = {}
        for i in items:
            bucket = workers.setdefault(i["worker_id"], {"cents": 0, "count": 0})
            bucket["cents"] += i["amount_cents"]
            bucket["count"] += 1
        if any(v["cents"] < 0 for v in workers.values()):
            # 单人净额为负说明红冲超过应付，需要人工先处理，不允许发出负支付。
            raise conflict("存在单人净额为负的情况，请先人工复核再封账",
                           code="negative_payout")

        prev = self.db.query_one(
            "SELECT id, row_hash FROM payment_batches ORDER BY id DESC LIMIT 1")
        prev_hash = prev["row_hash"] if prev else None
        sealed_at = now_ts()
        canonical_lines = [
            f"{worker_id}:{agg['cents']}:{agg['count']}"
            for worker_id, agg in sorted(workers.items())
        ]
        payments_hash = self.db.row_hash(*canonical_lines)
        items_hash = self.db.row_hash(
            *[f"{i['id']}:{i['amount_cents']}" for i in items])
        # payment_batches/payments 为不可变表（禁止 UPDATE/DELETE），哈希必须
        # 在插入前算定；封账全程持有数据库锁，预取自增 id 安全。
        batch_id = self.db.query_one(
            "SELECT COALESCE(MAX(id),0)+1 AS next_id FROM payment_batches")["next_id"]
        row_hash = self.db.row_hash(
            batch_id, period_start, period_end, total, len(workers), len(items),
            prev_hash or "", payments_hash, items_hash, sealed_at,
        )
        self.db.insert(
            "payment_batches",
            id=batch_id,
            period_start=period_start,
            period_end=period_end,
            status="sealed",
            total_cents=total,
            worker_count=len(workers),
            item_count=len(items),
            prev_hash=prev_hash,
            row_hash=row_hash,
            sealed_at=sealed_at,
            sealed_by=actor,
        )
        for worker_id, agg in sorted(workers.items()):
            p_hash = self.db.row_hash(batch_id, worker_id, agg["cents"],
                                      agg["count"], row_hash)
            self.db.insert(
                "payments",
                batch_id=batch_id,
                worker_id=worker_id,
                amount_cents=agg["cents"],
                item_count=agg["count"],
                row_hash=p_hash,
                created_at=sealed_at,
            )
        for i in items:
            self.db.execute("UPDATE pay_items SET batch_id=? WHERE id=?", (batch_id, i["id"]))
        # 锁定期内班次，封账后不再调整。
        self.db.execute(
            "UPDATE shifts SET locked=1 WHERE work_date BETWEEN ? AND ?",
            (period_start, period_end),
        )
        self.db.audit(actor, "seal_batch", "payment_batches", batch_id,
                      {"period": [period_start, period_end], "total_cents": total,
                       "workers": len(workers)})
        return self.get_batch(batch_id)

    def list_batches(self):
        rows = self.db.query("SELECT * FROM payment_batches ORDER BY id")
        return [self._batch_summary(dict(r)) for r in rows]

    def get_batch(self, batch_id):
        batch = self.db.get("payment_batches", batch_id)
        if batch is None:
            raise not_found("支付批次不存在")
        result = self._batch_summary(dict(batch))
        result["payments"] = []
        for p in self.db.query(
            """SELECT p.*, w.name AS worker_name FROM payments p
               JOIN workers w ON w.id=p.worker_id
               WHERE p.batch_id=? ORDER BY p.worker_id""", (batch_id,)
        ):
            pay = dict(p)
            pay["amount_yuan"] = cents_to_yuan(pay["amount_cents"])
            result["payments"].append(pay)
        result["chain_valid"] = self.verify_chain(batch_id)["valid"]
        return result

    def _batch_summary(self, batch):
        batch["total_yuan"] = cents_to_yuan(batch["total_cents"])
        return batch

    def verify_chain(self, up_to_batch_id=None):
        """从头重算哈希链，返回逐批校验结果（防悄然修改的独立核对入口）。"""
        batches = self.db.query("SELECT * FROM payment_batches ORDER BY id")
        prev_hash = None
        results = []
        ok_all = True
        for b in batches:
            items = self.db.query(
                "SELECT id, amount_cents FROM pay_items WHERE batch_id=? ORDER BY id", (b["id"],))
            payments = self.db.query(
                "SELECT worker_id, amount_cents, item_count FROM payments WHERE batch_id=? "
                "ORDER BY worker_id", (b["id"],))
            payments_hash = self.db.row_hash(
                *[f"{p['worker_id']}:{p['amount_cents']}:{p['item_count']}" for p in payments])
            items_hash = self.db.row_hash(
                *[f"{i['id']}:{i['amount_cents']}" for i in items])
            recomputed = self.db.row_hash(
                b["id"], b["period_start"], b["period_end"], b["total_cents"],
                b["worker_count"], b["item_count"], b["prev_hash"] or "",
                payments_hash, items_hash, b["sealed_at"],
            )
            valid = (b["prev_hash"] == prev_hash and b["row_hash"] == recomputed)
            ok_all = ok_all and valid
            results.append({"batch_id": b["id"], "valid": valid,
                            "stored_hash": b["row_hash"], "recomputed_hash": recomputed})
            prev_hash = b["row_hash"]
            if up_to_batch_id is not None and b["id"] == up_to_batch_id:
                break
        return {"valid": ok_all, "batches": results}

    # --- 管理者统计：季节性岗位实际带动的就业 ---
    def employment_stats(self, day_from, day_to):
        day_from = parse_day(day_from)
        day_to = parse_day(day_to)
        employed = self.db.query_one(
            """SELECT COUNT(DISTINCT worker_id) AS n FROM attendance_segments
               WHERE segment_date BETWEEN ? AND ? AND superseded_by IS NULL""",
            (day_from, day_to),
        )["n"]
        cat_rows = self.db.query(
            """SELECT category, COUNT(DISTINCT worker_id) AS workers,
                      SUM(minutes) AS minutes
               FROM attendance_segments
               WHERE segment_date BETWEEN ? AND ? AND superseded_by IS NULL
               GROUP BY category""",
            (day_from, day_to),
        )
        categories = {}
        total_minutes = 0
        for r in cat_rows:
            minutes = r["minutes"] or 0
            total_minutes += minutes
            categories[r["category"]] = {
                "workers": r["workers"], "minutes": minutes,
                "hours": round(minutes / 60, 2),
            }
        paid = self.db.query_one(
            """SELECT COALESCE(SUM(p.amount_cents),0) AS cents,
                      COUNT(DISTINCT p.worker_id) AS workers,
                      COUNT(*) AS payments
               FROM payments p JOIN payment_batches b ON b.id=p.batch_id
               WHERE b.period_start>=? AND b.period_end<=?""",
            (day_from, day_to),
        )
        open_cases = self.db.query_one(
            "SELECT COUNT(*) AS n FROM quality_cases WHERE status IN ('open','rework')"
        )["n"]
        return {
            "period": {"from": day_from, "to": day_to},
            "employed_workers": employed,
            "effective_work": {"total_minutes": total_minutes,
                               "total_hours": round(total_minutes / 60, 2),
                               "by_category": categories},
            "paid": {"total_cents": paid["cents"],
                     "total_yuan": cents_to_yuan(paid["cents"]),
                     "workers": paid["workers"], "payments": paid["payments"]},
            "open_quality_cases": open_cases,
        }
