"""计件产量录入与质检退回复核。

质检退回不会直接扣减个人报酬：退回数量对应的计件项进入 ``withheld``（暂缓）
状态并开立有证据的质量案件；复核后按决定恢复、返工或以独立红冲项核减，
原计件项始终保留可查。
"""

import json

from .errors import bad_request, conflict, not_found
from .timeutil import cents_to_yuan, now_ts, parse_day


class Production:
    def __init__(self, db):
        self.db = db

    def record_output(self, data, actor):
        worker = self.db.get("workers", data.get("worker_id"))
        if worker is None:
            raise not_found("人员不存在")
        operation = self.db.get("operations", data.get("operation_id"))
        if operation is None:
            raise not_found("工序不存在")
        quantity = data.get("quantity")
        if not isinstance(quantity, int) or quantity <= 0:
            raise bad_request("quantity 必须为正整数")
        work_date = parse_day(data.get("work_date"))
        segment_id = data.get("segment_id")
        if segment_id is not None:
            seg = self.db.get("attendance_segments", segment_id)
            if seg is None or seg["worker_id"] != worker["id"]:
                raise bad_request("关联出勤段不存在或不属于该人员")
        client_event_id = data.get("client_event_id")
        if client_event_id:
            old = self.db.query_one(
                "SELECT * FROM production_records WHERE client_event_id=?", (client_event_id,)
            )
            if old:
                item = self.db.query_one(
                    "SELECT * FROM pay_items WHERE ref_type='production' AND ref_id=?",
                    (old["id"],),
                )
                return {"accepted": False, "duplicate": True, "id": old["id"],
                        "pay_item": self._item(dict(item)),
                        "message": "产量记录已存在，未重复计件"}

        record_id = self.db.insert(
            "production_records",
            worker_id=worker["id"],
            operation_id=operation["id"],
            segment_id=segment_id,
            quantity=quantity,
            work_date=work_date,
            recorded_at=now_ts(),
            recorded_by=actor,
            client_event_id=client_event_id,
        )
        amount = quantity * operation["piece_rate_cents"]
        item_id = self.db.insert(
            "pay_items",
            worker_id=worker["id"],
            work_date=work_date,
            category="piecework",
            ref_type="production",
            ref_id=record_id,
            minutes=None,
            quantity=quantity,
            unit_rate_cents=operation["piece_rate_cents"],
            amount_cents=amount,
            note=f"计件产量 ×{quantity}",
            created_at=now_ts(),
        )
        self.db.audit(actor, "record_output", "production_records", record_id,
                      {"worker_id": worker["id"], "quantity": quantity,
                       "amount_cents": amount})
        return {"accepted": True, "duplicate": False, "id": record_id,
                "pay_item": self._item(dict(self.db.get("pay_items", item_id)))}

    def list_output(self, worker_id=None, day_from=None, day_to=None):
        sql = "SELECT * FROM production_records WHERE 1=1"
        params = []
        if worker_id is not None:
            sql += " AND worker_id=?"
            params.append(worker_id)
        if day_from:
            sql += " AND work_date>=?"
            params.append(parse_day(day_from))
        if day_to:
            sql += " AND work_date<=?"
            params.append(parse_day(day_to))
        return [dict(r) for r in self.db.query(sql + " ORDER BY id", params)]

    # --- 质检案件 ---
    def open_case(self, data, actor):
        record = self.db.get("production_records", data.get("production_record_id"))
        if record is None:
            raise not_found("产量记录不存在")
        quantity = data.get("quantity")
        if not isinstance(quantity, int) or quantity <= 0 or quantity > record["quantity"]:
            raise bad_request("退回数量必须为不超过产量的正整数")
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise bad_request("退回原因必填")

        item = self.db.query_one(
            "SELECT * FROM pay_items WHERE ref_type='production' AND ref_id=? AND version=1",
            (record["id"],),
        )
        if item is None:
            raise bad_request("该产量没有可复核的计件项")
        item = dict(item)
        if item["status"] != "active":
            raise conflict("该产量已在质量复核流程中，不能重复立案", code="case_exists")
        if item["batch_id"] is not None:
            raise conflict("该产量报酬已随批次封账，退回请走申诉与下期调整流程",
                           code="sealed")
        operation = self.db.get("operations", record["operation_id"])
        withheld = quantity * operation["piece_rate_cents"]

        case_id = self.db.insert(
            "quality_cases",
            production_record_id=record["id"],
            worker_id=record["worker_id"],
            quantity=quantity,
            reason=reason,
            evidence=data.get("evidence"),
            status="open",
            withheld_cents=withheld,
            opened_at=now_ts(),
        )
        # 不直接扣款：原计件项整体冲销留痕，再按"合格数量 / 争议数量"拆成
        # 两条新项——合格部分继续有效，争议部分暂缓等待复核。
        self.db.execute(
            "UPDATE pay_items SET status='reversed', "
            "withheld_reason=? WHERE id=?",
            (f"质量案件 #{case_id} 立案拆项（原项留痕）", item["id"]),
        )
        accepted_qty = record["quantity"] - quantity
        if accepted_qty > 0:
            self.db.insert(
                "pay_items",
                worker_id=record["worker_id"],
                work_date=item["work_date"],
                category="piecework",
                ref_type="production",
                ref_id=record["id"],
                minutes=None,
                quantity=accepted_qty,
                unit_rate_cents=item["unit_rate_cents"],
                amount_cents=accepted_qty * item["unit_rate_cents"],
                note=f"质量案件 #{case_id} 合格数量 {accepted_qty} 件"
                     f"（原计件项 #{item['id']} 留痕）",
                version=2,
                supersedes=item["id"],
                created_at=now_ts(),
            )
        self.db.insert(
            "pay_items",
            worker_id=record["worker_id"],
            work_date=item["work_date"],
            category="piecework",
            ref_type="production",
            ref_id=record["id"],
            minutes=None,
            quantity=quantity,
            unit_rate_cents=item["unit_rate_cents"],
            amount_cents=withheld,
            note=f"质量案件 #{case_id} 争议数量 {quantity} 件，暂缓待复核",
            status="withheld",
            withheld_reason=f"质量案件 #{case_id} 复核中",
            quality_case_id=case_id,
            version=3,
            supersedes=item["id"],
            created_at=now_ts(),
        )
        self._case_event(case_id, "open",
                         {"quantity": quantity, "reason": reason,
                          "withheld_cents": withheld}, actor)
        self.db.audit(actor, "open_quality_case", "quality_cases", case_id,
                      {"production_record_id": record["id"], "quantity": quantity})
        return self.get_case(case_id)

    def decide_case(self, case_id, data, actor):
        case = self.db.get("quality_cases", case_id)
        if case is None:
            raise not_found("质量案件不存在")
        if case["status"] != "open" and data.get("decision") != "note":
            raise conflict("案件已作出决定，不能更改；可追加说明")
        decision = data.get("decision")
        if decision not in ("upheld", "rejected", "rework", "note"):
            raise bad_request("decision 必须是 upheld、rejected、rework 或 note")
        note = (data.get("note") or "").strip()
        if decision != "note" and not note:
            raise bad_request("复核决定必须附说明（依据）")

        item = self.db.query_one(
            "SELECT * FROM pay_items WHERE quality_case_id=? "
            "ORDER BY version DESC, id DESC LIMIT 1", (case_id,)
        )
        item = dict(item) if item else None
        result_payload = {"decision": decision, "note": note}

        if decision == "upheld":
            # 责任成立：争议项冲销留痕，另开等负额红冲项——不静默删除任何记录。
            # 合格部分在立案拆项时已是有效项，无需再动。
            self.db.execute(
                "UPDATE quality_cases SET status='upheld', decided_at=?, decided_by=?, "
                "decision_note=? WHERE id=?",
                (now_ts(), actor, note, case_id),
            )
            if item and item["status"] == "withheld":
                # 争议项原属暂缓（从未计入应付）：先恢复为有效，再以独立负向
                # 红冲项核减。两条记录都在村民逐项清单中可见，净额等于合格
                # 数量的报酬，且核减动作有案件证据支撑。
                self.db.execute(
                    "UPDATE pay_items SET status='active', withheld_reason=? WHERE id=?",
                    (f"质量案件 #{case_id} 责任成立，转红冲核减：{note}", item["id"]),
                )
                adjust_id = self.db.insert(
                    "pay_items",
                    worker_id=case["worker_id"],
                    work_date=item["work_date"],
                    category="quality_adjust",
                    ref_type="quality_adjust",
                    ref_id=case_id,
                    minutes=None,
                    quantity=case["quantity"],
                    unit_rate_cents=0,
                    amount_cents=-case["withheld_cents"],
                    note=f"质量案件 #{case_id} 退回 {case['quantity']} 件核减"
                         f"（争议计件项 #{item['id']} 留痕）",
                    quality_case_id=case_id,
                    created_at=now_ts(),
                )
                result_payload["adjustment_pay_item_id"] = adjust_id
        elif decision == "rejected":
            # 退回不成立：争议数量恢复有效，全额照付。
            self.db.execute(
                "UPDATE quality_cases SET status='rejected', decided_at=?, decided_by=?, "
                "decision_note=? WHERE id=?",
                (now_ts(), actor, note, case_id),
            )
            if item and item["status"] == "withheld":
                self.db.execute(
                    "UPDATE pay_items SET status='active', withheld_reason=NULL WHERE id=?",
                    (item["id"],),
                )
        elif decision == "rework":
            # 返工安排：争议项继续暂缓，等待返工完成确认。
            self.db.execute(
                "UPDATE quality_cases SET status='rework', decided_at=?, decided_by=?, "
                "decision_note=? WHERE id=?",
                (now_ts(), actor, note, case_id),
            )
        elif decision == "note":
            pass

        self._case_event(case_id, "decide" if decision != "note" else "note",
                         result_payload, actor)
        return self.get_case(case_id)

    def complete_rework(self, case_id, data, actor):
        case = self.db.get("quality_cases", case_id)
        if case is None:
            raise not_found("质量案件不存在")
        if case["status"] != "rework":
            raise conflict("只有处于返工状态的案件可以登记返工完成")
        note = (data.get("note") or "返工完成，复验合格").strip()
        self.db.execute(
            "UPDATE quality_cases SET status='rejected', decided_at=?, "
            "decision_note=? WHERE id=?",
            (now_ts(), f"返工完成：{note}", case_id),
        )
        item = self.db.query_one(
            "SELECT * FROM pay_items WHERE quality_case_id=? AND status='withheld'",
            (case_id,),
        )
        if item:
            self.db.execute(
                "UPDATE pay_items SET status='active', withheld_reason=NULL WHERE id=?",
                (item["id"],),
            )
        self._case_event(case_id, "rework_done", {"note": note}, actor)
        return self.get_case(case_id)

    def list_cases(self, worker_id=None, status=None):
        sql = "SELECT * FROM quality_cases WHERE 1=1"
        params = []
        if worker_id is not None:
            sql += " AND worker_id=?"
            params.append(worker_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        return [self._case_dict(self.db.get("quality_cases", r["id"]))
                for r in self.db.query(sql + " ORDER BY id", params)]

    def get_case(self, case_id):
        case = self.db.get("quality_cases", case_id)
        if case is None:
            raise not_found("质量案件不存在")
        return self._case_dict(case)

    def _case_dict(self, case):
        result = dict(case)
        result["withheld_yuan"] = cents_to_yuan(case["withheld_cents"])
        result["events"] = [
            dict(e) for e in self.db.query(
                "SELECT * FROM quality_case_events WHERE case_id=? ORDER BY id", (case["id"],)
            )
        ]
        for event in result["events"]:
            event["payload"] = json.loads(event["payload"])
        return result

    def _case_event(self, case_id, event_type, payload, actor):
        self.db.insert(
            "quality_case_events",
            case_id=case_id,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
            actor=actor,
            created_at=now_ts(),
        )

    def _item(self, item):
        item["amount_yuan"] = cents_to_yuan(item["amount_cents"])
        item["unit_rate_yuan"] = cents_to_yuan(item["unit_rate_cents"])
        return item
