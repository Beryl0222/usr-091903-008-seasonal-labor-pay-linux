"""HTTP API：角色鉴权 + JSON 路由。

令牌（Authorization: Bearer <token>）：
  admin   ADMIN_TOKEN（默认 dev-admin-token）
  班组长  LEADER_TOKEN（默认 dev-leader-token）
  财务    FINANCE_TOKEN（默认 dev-finance-token）
  质检    QC_TOKEN（默认 dev-qc-token）
  扫码器  SCANNER_TOKEN（默认 dev-scanner-token）
  村民    worker-<worker_id>（建档时发放，只能访问本人数据）

设计取舍：所有调用经同一把服务锁串行化。用工计酬是低频强一致场景，
宁可简单可核对，不做读后写并发优化。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from commands import (
    ConflictError,
    DomainError,
    NotFoundError,
    PayService,
)

ROLE_TOKENS = {
    "admin": "dev-admin-token",
    "leader": "dev-leader-token",
    "finance": "dev-finance-token",
    "qc": "dev-qc-token",
    "scanner": "dev-scanner-token",
}

# 路由：(method, compiled_path, 允许角色, handler 名)
# path 中 {name} 捕获为 kwargs
ROUTES: list[tuple[str, str, tuple, str]] = [
    # 基础档案（人事/admin）
    ("POST", r"/admin/workers", ("admin",), "create_worker"),
    ("GET", r"/admin/workers", ("admin", "leader"), "list_workers"),
    ("POST", r"/admin/workers/(?P<worker_id>[\w\-]+)/training", ("admin",), "record_training"),
    ("POST", r"/admin/workers/(?P<worker_id>[\w\-]+)/deactivate", ("admin",), "deactivate_worker"),
    ("POST", r"/admin/workers/(?P<worker_id>[\w\-]+)/health-cert", ("admin",), "record_health_cert"),
    ("POST", r"/admin/operations", ("admin",), "create_operation"),
    ("GET", r"/admin/operations", ("admin", "leader", "qc"), "list_operations"),
    ("GET", r"/admin/events/verify", ("admin",), "verify_chain"),

    # 班组长
    ("POST", r"/leader/shifts", ("leader",), "create_shift"),
    ("GET", r"/leader/shifts", ("leader", "admin"), "list_shifts"),
    ("GET", r"/leader/eligibility", ("leader",), "check_eligibility"),
    ("POST", r"/leader/shifts/(?P<shift_id>[\w\-]+)/assign", ("leader",), "assign_worker"),
    ("POST", r"/leader/shifts/(?P<shift_id>[\w\-]+)/substitute", ("leader",), "substitute_worker"),
    ("POST", r"/leader/incidents", ("leader",), "record_incident"),
    ("POST", r"/leader/punches", ("leader", "scanner"), "record_punch_confirm"),

    # 扫码器
    ("POST", r"/scanner/punches", ("scanner",), "record_punch_scanner"),

    # 质检与责任复核
    ("POST", r"/qc/quantities", ("qc", "leader"), "report_quantity"),
    ("GET", r"/qc/quantities", ("qc", "admin"), "list_quantities"),
    ("POST", r"/qc/quantities/(?P<quantity_id>[\w\-]+)/decision", ("qc",), "qc_decide"),
    ("POST", r"/qc/reviews", ("qc",), "open_review"),
    ("GET", r"/qc/reviews", ("qc", "admin"), "list_reviews"),
    ("POST", r"/qc/reviews/(?P<review_id>[\w\-]+)/conclude", ("qc", "admin"), "conclude_review"),

    # 村民（本人）
    ("GET", r"/workers/(?P<worker_id>[\w\-]+)/payroll", ("worker", "admin", "finance", "leader"), "worker_payroll"),
    ("POST", r"/workers/(?P<worker_id>[\w\-]+)/appeals", ("worker",), "open_appeal"),
    ("GET", r"/workers/(?P<worker_id>[\w\-]+)/appeals", ("worker", "admin"), "list_worker_appeals"),

    # 申诉处理 / 调整项
    ("GET", r"/admin/appeals", ("admin", "qc", "leader"), "list_appeals"),
    ("POST", r"/admin/appeals/(?P<appeal_id>[\w\-]+)/resolve", ("admin",), "resolve_appeal"),
    ("POST", r"/finance/adjustments", ("finance", "admin"), "record_adjustment"),

    # 计酬与封账
    ("GET", r"/payroll/compute", ("finance", "admin"), "compute_payroll"),
    ("POST", r"/finance/payroll/close", ("finance",), "close_payroll"),
    ("GET", r"/finance/payroll/batches", ("finance", "admin"), "list_batches"),
    ("GET", r"/finance/payroll/batches/(?P<batch_id>[\w\-]+)", ("finance", "admin"), "get_batch"),
    ("GET", r"/finance/payroll/batches/(?P<batch_id>[\w\-]+)/verify", ("finance", "admin"), "verify_batch"),

    # 管理者统计
    ("GET", r"/admin/stats/seasonal", ("admin",), "seasonal_stats"),
]


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class Api:
    def __init__(self, service: PayService, tokens: dict | None = None, health: dict | None = None):
        self.service = service
        self.tokens = dict(tokens or ROLE_TOKENS)
        self.token_to_role = {token: role for role, token in self.tokens.items()}
        self.health = health or {"status": "ok"}
        self.lock = threading.RLock()

    # ---------------- 鉴权 ----------------

    def authenticate(self, auth_header: str | None) -> tuple[str, str]:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthError(401, "缺少 Bearer 令牌")
        token = auth_header[len("Bearer "):].strip()
        if token in self.token_to_role:
            return self.token_to_role[token], token
        if token.startswith("worker-"):
            worker_id = token[len("worker-"):]
            if worker_id:
                return "worker", worker_id
        raise AuthError(401, "令牌无效")

    def authorize(self, role: str, identity: str, allowed: tuple, kwargs: dict):
        if role not in allowed:
            raise AuthError(403, f"角色 {role} 无权访问该接口")
        if role == "worker" and "worker_id" in kwargs and kwargs["worker_id"] != identity:
            raise AuthError(403, "村民只能访问本人数据")

    # ---------------- 分发 ----------------

    def handle(self, method: str, path: str, query: dict, body: dict,
               auth_header: str | None) -> tuple[int, dict]:
        # 先定位路由：未知路径 404 优先于鉴权，避免暴露接口存在性差异
        chosen = None
        path_exists = False
        for route_method, pattern, allowed, action in ROUTES:
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            path_exists = True
            if route_method == method:
                chosen = (match, allowed, action)
                break
        if chosen is None:
            raise AuthError(405, "方法不允许") if path_exists else AuthError(404, "接口不存在")
        match, allowed, action = chosen
        kwargs = match.groupdict()
        role, identity = self.authenticate(auth_header)
        self.authorize(role, identity, allowed, kwargs)
        handler = getattr(self, f"_do_{action}")
        with self.lock:
            return handler(role, identity, query, body, **kwargs)

    # ---------------- 处理器 ----------------

    @staticmethod
    def _actor(role: str, identity: str) -> str:
        return f"{role}:{identity}"

    @staticmethod
    def _need(body: dict, key: str):
        if key not in body or body[key] in (None, ""):
            raise DomainError(f"缺少参数: {key}")
        return body[key]

    @staticmethod
    def _opt(body: dict, key: str, default=None):
        value = body.get(key, default)
        return None if value == "" else value

    @staticmethod
    def _q(query: dict, key: str, default=None):
        values = query.get(key)
        return values[0] if values else default

    def _do_create_worker(self, role, identity, query, body, **kw):
        worker_id = self._need(body, "worker_id")
        event = self.service.register_worker(
            worker_id, self._need(body, "name"), self._actor(role, identity),
            id_card_hint=body.get("id_card_hint", ""), phone=body.get("phone", ""))
        return 201, {"event": event, "worker_token": f"worker-{worker_id}"}

    def _do_list_workers(self, role, identity, query, body, **kw):
        return 200, {"workers": self.service.list_workers()}

    def _do_record_training(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.record_training(
            kw["worker_id"], self._need(body, "course_id"),
            self._need(body, "passed_at"), self._actor(role, identity),
            expires_at=self._opt(body, "expires_at"),
            course_name=self._opt(body, "course_name"))}

    def _do_deactivate_worker(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.deactivate_worker(
            kw["worker_id"], self._actor(role, identity))}

    def _do_record_health_cert(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.record_health_cert(
            kw["worker_id"], self._need(body, "expires_at"), self._actor(role, identity),
            issued_at=self._opt(body, "issued_at"), cert_id=self._opt(body, "cert_id"))}

    def _do_create_operation(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.create_operation(
            self._need(body, "operation_id"), self._actor(role, identity),
            name=self._opt(body, "name"), hourly_wage=self._opt(body, "hourly_wage"),
            piece_unit_pay=self._opt(body, "piece_unit_pay"),
            required_trainings=body.get("required_trainings", []),
            seasonal=body.get("seasonal", True))}

    def _do_list_operations(self, role, identity, query, body, **kw):
        return 200, {"operations": self.service.list_operations()}

    def _do_verify_chain(self, role, identity, query, body, **kw):
        return 200, self.service.verify_chain(disk=self._q(query, "disk") == "1")

    def _do_create_shift(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.create_shift(
            self._need(body, "shift_id"), self._need(body, "operation_id"),
            self._need(body, "start"), self._need(body, "end"),
            self._actor(role, identity), leader_id=self._opt(body, "leader_id"))}

    def _do_list_shifts(self, role, identity, query, body, **kw):
        return 200, {"shifts": self.service.list_shifts()}

    def _do_check_eligibility(self, role, identity, query, body, **kw):
        return 200, self.service.check_eligibility(
            self._q(query, "worker_id") or self._need(body, "worker_id"),
            self._q(query, "operation_id") or self._need(body, "operation_id"),
            self._q(query, "at") or self._need(body, "at"))

    def _do_assign_worker(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.assign_worker(
            kw["shift_id"], self._need(body, "worker_id"), self._actor(role, identity),
            assignment_id=self._opt(body, "assignment_id"),
            operation_id=self._opt(body, "operation_id"))}

    def _do_substitute_worker(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.substitute_worker(
            kw["shift_id"], self._need(body, "base_assignment_id"),
            self._need(body, "replacement_worker_id"),
            self._need(body, "start"), self._need(body, "end"),
            self._actor(role, identity), reason=body.get("reason", "temporary_sub"),
            operation_id=self._opt(body, "operation_id"))}

    def _do_record_incident(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.record_incident(
            self._need(body, "shift_id"), self._need(body, "worker_id"),
            self._need(body, "kind"), self._need(body, "start"),
            self._need(body, "end"), self._actor(role, identity),
            note=body.get("note", ""), incident_id=self._opt(body, "incident_id"))}

    def _punch(self, role, identity, body, default_source):
        return 201, {"event": self.service.record_punch(
            self._need(body, "worker_id"), self._need(body, "dir"),
            self._need(body, "ts"), self._actor(role, identity),
            scanner_id=self._opt(body, "scanner_id"),
            source=body.get("source", default_source),
            client_event_id=self._opt(body, "client_event_id"),
            punch_id=self._opt(body, "punch_id"))}

    def _do_record_punch_scanner(self, role, identity, query, body, **kw):
        return self._punch(role, identity, body, "scanner")

    def _do_record_punch_confirm(self, role, identity, query, body, **kw):
        return self._punch(role, identity, body,
                           "leader_confirm" if role == "leader" else "scanner")

    def _do_report_quantity(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.report_quantity(
            self._need(body, "worker_id"), self._need(body, "operation_id"),
            self._need(body, "shift_id"), int(self._need(body, "quantity")),
            self._actor(role, identity), unit_pay=self._opt(body, "unit_pay"),
            quantity_id=self._opt(body, "quantity_id"))}

    def _do_list_quantities(self, role, identity, query, body, **kw):
        return 200, {"quantities": self.service.list_quantities(self._q(query, "worker_id"))}

    def _do_qc_decide(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.qc_decide(
            kw["quantity_id"], self._need(body, "result"), self._actor(role, identity),
            evidence=body.get("evidence", ""))}

    def _do_open_review(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.open_review(
            self._need(body, "quantity_id"), self._actor(role, identity),
            evidence=self._opt(body, "evidence"), review_id=self._opt(body, "review_id"))}

    def _do_list_reviews(self, role, identity, query, body, **kw):
        return 200, {"reviews": self.service.list_reviews()}

    def _do_conclude_review(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.conclude_review(
            kw["review_id"], self._need(body, "responsibility"),
            self._actor(role, identity), accepted_qty=self._opt(body, "accepted_qty"),
            note=body.get("note", ""), evidence=body.get("evidence", ""))}

    def _do_worker_payroll(self, role, identity, query, body, **kw):
        return 200, self.service.worker_payroll(
            kw["worker_id"], self._q(query, "period_start") or self._need(body, "period_start"),
            self._q(query, "period_end") or self._need(body, "period_end"))

    def _do_open_appeal(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.open_appeal(
            kw["worker_id"], self._need(body, "reason"), self._actor(role, identity),
            scope_type=body.get("scope_type", "payroll"),
            scope_id=self._opt(body, "scope_id"), appeal_id=self._opt(body, "appeal_id"))}

    def _do_list_worker_appeals(self, role, identity, query, body, **kw):
        return 200, {"appeals": self.service.list_appeals(kw["worker_id"])}

    def _do_list_appeals(self, role, identity, query, body, **kw):
        return 200, {"appeals": self.service.list_appeals()}

    def _do_resolve_appeal(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.resolve_appeal(
            kw["appeal_id"], self._need(body, "decision"), self._actor(role, identity),
            note=body.get("note", ""), grant_amount=self._opt(body, "grant_amount"),
            grant_reason=self._opt(body, "grant_reason"),
            work_date=self._opt(body, "work_date"))}

    def _do_record_adjustment(self, role, identity, query, body, **kw):
        return 201, {"event": self.service.record_adjustment(
            self._need(body, "worker_id"), self._need(body, "kind"),
            float(self._need(body, "amount")), self._need(body, "reason"),
            self._need(body, "work_date"), self._actor(role, identity),
            evidence=body.get("evidence", ""), adjustment_id=self._opt(body, "adjustment_id"))}

    def _do_compute_payroll(self, role, identity, query, body, **kw):
        start = self._q(query, "period_start") or self._need(body, "period_start")
        end = self._q(query, "period_end") or self._need(body, "period_end")
        return 200, self.service.compute_payroll(start, end)

    def _do_close_payroll(self, role, identity, query, body, **kw):
        return 201, {"batch": self.service.close_payroll(
            self._need(body, "name"), self._need(body, "period_start"),
            self._need(body, "period_end"), self._actor(role, identity),
            allow_provisional=body.get("allow_provisional", False))}

    def _do_list_batches(self, role, identity, query, body, **kw):
        return 200, {"batches": self.service.list_batches()}

    def _do_get_batch(self, role, identity, query, body, **kw):
        return 200, {"batch": self.service.get_batch(kw["batch_id"])}

    def _do_verify_batch(self, role, identity, query, body, **kw):
        return 200, self.service.verify_batch(kw["batch_id"])

    def _do_seasonal_stats(self, role, identity, query, body, **kw):
        start = self._q(query, "period_start") or self._need(body, "period_start")
        end = self._q(query, "period_end") or self._need(body, "period_end")
        return 200, self.service.seasonal_stats(start, end)


def make_handler(api: Api):
    class ApiHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle_request(self, method: str):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body: dict = {}
            if raw:
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise ValueError
                    body = decoded
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "请求体必须是 JSON 对象"})
                    return
            query = parse_qs(parsed.query)
            try:
                status, payload = api.handle(
                    method, parsed.path, query, body,
                    self.headers.get("Authorization"))
                self._send(status, payload)
            except AuthError as error:
                self._send(error.status, {"error": error.message})
            except NotFoundError as error:
                self._send(404, {"error": str(error)})
            except ConflictError as error:
                self._send(409, {"error": str(error)})
            except DomainError as error:
                self._send(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 - 兜底，避免栈泄漏到响应
                self._send(500, {"error": f"服务器内部错误: {error}"})

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                self._send(200, api.health)
                return
            self._handle_request("GET")

        def do_POST(self):
            self._handle_request("POST")

        def log_message(self, *_args):
            return

    return ApiHandler
