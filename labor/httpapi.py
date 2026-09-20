"""HTTP 路由、Bearer 令牌鉴权与 JSON 序列化。

角色：hr（人事）、production（生产/质检）、leader（班组长）、
finance（财务）、admin、worker（村民，只能访问本人数据）。
"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .errors import ApiError, forbidden, unauthorized
from .timeutil import cents_to_yuan, yuan_to_cents

STAFF = ("hr", "production", "leader", "finance", "admin")


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _worker_or_staff(auth, worker_id):
    if auth["role"] in STAFF:
        return
    if auth["role"] == "worker" and auth.get("worker_id") == worker_id:
        return
    raise forbidden("只能访问本人数据")


def create_handler(service):
    routes = []

    def route(method, pattern):
        regex = re.compile("^" + pattern + "$")

        def register(fn):
            routes.append((method, regex, fn))
            return fn
        return register

    # ---------- 人员 ----------
    @route("POST", r"/api/workers")
    def create_worker(req, auth, body):
        req.require(auth, ("hr", "admin"))
        return service.registry.create_worker(body, req.actor)

    @route("GET", r"/api/workers")
    def list_workers(req, auth, body):
        req.require(auth, STAFF)
        return service.registry.list_workers()

    @route("GET", r"/api/workers/(?P<worker_id>\d+)")
    def get_worker(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        row = service.db.get("workers", wid)
        if row is None:
            from .errors import not_found
            raise not_found("人员不存在")
        return dict(row)

    @route("POST", r"/api/workers/(?P<worker_id>\d+)/terminate")
    def terminate_worker(req, auth, body, worker_id):
        req.require(auth, ("hr", "admin"))
        return service.registry.terminate_worker(int(worker_id), req.actor)

    @route("POST", r"/api/workers/(?P<worker_id>\d+)/trainings")
    def add_training(req, auth, body, worker_id):
        req.require(auth, ("hr", "admin"))
        return service.registry.add_training(int(worker_id), body, req.actor)

    @route("POST", r"/api/workers/(?P<worker_id>\d+)/health-certificates")
    def add_health(req, auth, body, worker_id):
        req.require(auth, ("hr", "admin"))
        return service.registry.add_health_certificate(int(worker_id), body, req.actor)

    @route("POST", r"/api/workers/(?P<worker_id>\d+)/operations/(?P<op_id>\d+)/grant")
    def grant(req, auth, body, worker_id, op_id):
        req.require(auth, ("hr", "production", "admin"))
        return service.registry.grant_operation(int(worker_id), int(op_id), req.actor)

    @route("GET", r"/api/workers/(?P<worker_id>\d+)/eligibility")
    def eligibility(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        op = req.query.get("operation_id")
        day = req.query.get("day")
        return service.scheduling.check_worker(
            wid, int(op) if op else None, day)

    # ---------- 村民视图 ----------
    @route("GET", r"/api/workers/(?P<worker_id>\d+)/payroll")
    def worker_payroll(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        return service.payroll.worker_payroll(
            wid, req.query.get("from"), req.query.get("to"))

    @route("GET", r"/api/workers/(?P<worker_id>\d+)/segments")
    def worker_segments(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        return service.attendance.list_segments(
            wid, req.query.get("from"), req.query.get("to"),
            include_superseded=req.query.get("include_superseded") == "1")

    @route("GET", r"/api/workers/(?P<worker_id>\d+)/punches")
    def worker_punches(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        return service.attendance.list_punches(
            wid, req.query.get("from"), req.query.get("to"))

    @route("GET", r"/api/workers/(?P<worker_id>\d+)/quality-cases")
    def worker_cases(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        return service.production.list_cases(wid, req.query.get("status"))

    # ---------- 工序 ----------
    @route("POST", r"/api/operations")
    def create_operation(req, auth, body):
        req.require(auth, ("production", "admin"))
        return service.registry.create_operation(body, req.actor)

    @route("GET", r"/api/operations")
    def list_operations(req, auth, body):
        req.require(auth, None)
        return service.registry.list_operations()

    # ---------- 班次 ----------
    @route("POST", r"/api/shifts")
    def create_shift(req, auth, body):
        req.require(auth, ("leader", "production", "admin"))
        return service.scheduling.create_shift(body, req.actor)

    @route("GET", r"/api/shifts")
    def list_shifts(req, auth, body):
        req.require(auth, STAFF)
        return service.scheduling.list_shifts(req.query.get("date"))

    @route("GET", r"/api/shifts/(?P<shift_id>\d+)")
    def get_shift(req, auth, body, shift_id):
        req.require(auth, STAFF)
        return service.scheduling.get_shift(int(shift_id))

    @route("POST", r"/api/shifts/(?P<shift_id>\d+)/assignments")
    def assign(req, auth, body, shift_id):
        req.require(auth, ("leader", "production", "admin"))
        return service.scheduling.assign_worker(int(shift_id), body, req.actor)

    # ---------- 打卡与出勤 ----------
    @route("POST", r"/api/punches")
    def punch(req, auth, body):
        # 扫码器由班组长账号代发；村民本人也可自助打卡。
        req.require(auth, ("leader", "production", "worker"))
        if auth["role"] == "worker":
            body.setdefault("worker_id", auth["worker_id"])
            if body.get("worker_id") != auth["worker_id"]:
                raise forbidden("不能替他人打卡")
        return service.attendance.record_punch(body, req.actor)

    @route("GET", r"/api/punches")
    def list_punches(req, auth, body):
        req.require(auth, STAFF)
        wid = req.query.get("worker_id")
        return service.attendance.list_punches(
            int(wid) if wid else None,
            req.query.get("from"), req.query.get("to"))

    @route("POST", r"/api/attendance-reports")
    def report(req, auth, body):
        req.require(auth, ("leader", "production", "admin"))
        return service.attendance.submit_report(body, req.actor)

    @route("GET", r"/api/attendance-segments")
    def segments(req, auth, body):
        req.require(auth, STAFF)
        wid = req.query.get("worker_id")
        return service.attendance.list_segments(
            int(wid) if wid else None,
            req.query.get("from"), req.query.get("to"),
            include_superseded=req.query.get("include_superseded") == "1")

    # ---------- 产量与质检 ----------
    @route("POST", r"/api/production-records")
    def production_record(req, auth, body):
        req.require(auth, ("leader", "production", "admin"))
        return service.production.record_output(body, req.actor)

    @route("GET", r"/api/production-records")
    def production_list(req, auth, body):
        req.require(auth, STAFF)
        wid = req.query.get("worker_id")
        return service.production.list_output(
            int(wid) if wid else None,
            req.query.get("from"), req.query.get("to"))

    @route("POST", r"/api/quality-cases")
    def open_case(req, auth, body):
        req.require(auth, ("production", "admin"))
        return service.production.open_case(body, req.actor)

    @route("GET", r"/api/quality-cases")
    def list_cases(req, auth, body):
        req.require(auth, STAFF)
        wid = req.query.get("worker_id")
        return service.production.list_cases(
            int(wid) if wid else None, req.query.get("status"))

    @route("GET", r"/api/quality-cases/(?P<case_id>\d+)")
    def get_case(req, auth, body, case_id):
        req.require(auth, STAFF)
        case = service.production.get_case(int(case_id))
        _worker_or_staff(auth, case["worker_id"])
        return case

    @route("POST", r"/api/quality-cases/(?P<case_id>\d+)/decision")
    def decide_case(req, auth, body, case_id):
        req.require(auth, ("production", "admin"))
        return service.production.decide_case(int(case_id), body, req.actor)

    @route("POST", r"/api/quality-cases/(?P<case_id>\d+)/rework-complete")
    def rework_done(req, auth, body, case_id):
        req.require(auth, ("production", "leader", "admin"))
        return service.production.complete_rework(int(case_id), body, req.actor)

    # ---------- 申诉 ----------
    @route("POST", r"/api/workers/(?P<worker_id>\d+)/pay-adjustments")
    def create_adjustment(req, auth, body, worker_id):
        req.require(auth, ("finance", "hr", "admin"))
        return service.payroll.create_adjustment(int(worker_id), body, req.actor)

    @route("POST", r"/api/workers/(?P<worker_id>\d+)/appeals")
    def submit_appeal(req, auth, body, worker_id):
        wid = int(worker_id)
        if not (auth["role"] in ("hr", "admin")
                or (auth["role"] == "worker" and auth.get("worker_id") == wid)):
            raise forbidden("只能由本人或人事提交申诉")
        return service.appeals.submit(wid, body, req.actor)

    @route("GET", r"/api/workers/(?P<worker_id>\d+)/appeals")
    def worker_appeals(req, auth, body, worker_id):
        wid = int(worker_id)
        _worker_or_staff(auth, wid)
        return service.appeals.list_for_worker(wid)

    @route("GET", r"/api/appeals")
    def all_appeals(req, auth, body):
        req.require(auth, STAFF)
        return service.appeals.list_all(req.query.get("status"))

    @route("POST", r"/api/appeals/(?P<appeal_id>\d+)/(?P<action>accept|resolve|reject)")
    def appeal_action(req, auth, body, appeal_id, action):
        return service.appeals.transition(
            int(appeal_id), action, body.get("message"), req.actor, auth["role"])

    @route("POST", r"/api/appeals/(?P<appeal_id>\d+)/notes")
    def appeal_note(req, auth, body, appeal_id):
        wid = auth.get("worker_id") if auth["role"] == "worker" else None
        return service.appeals.add_note(
            int(appeal_id), body.get("message"), req.actor, worker_id=wid)

    # ---------- 封账与统计 ----------
    @route("POST", r"/api/payment-batches/seal")
    def seal(req, auth, body):
        req.require(auth, ("finance", "admin"))
        return service.payroll.seal_batch(body, req.actor)

    @route("GET", r"/api/payment-batches/verify-chain")
    def verify_chain(req, auth, body):
        req.require(auth, ("finance", "admin"))
        return service.payroll.verify_chain()

    @route("GET", r"/api/payment-batches")
    def list_batches(req, auth, body):
        req.require(auth, ("finance", "admin"))
        return service.payroll.list_batches()

    @route("GET", r"/api/payment-batches/(?P<batch_id>\d+)")
    def get_batch(req, auth, body, batch_id):
        req.require(auth, ("finance", "admin"))
        return service.payroll.get_batch(int(batch_id))

    @route("GET", r"/api/stats/employment")
    def employment(req, auth, body):
        req.require(auth, ("admin", "production", "finance"))
        return service.payroll.employment_stats(
            req.query.get("from"), req.query.get("to"))

    # ---------- 参数 ----------
    @route("PUT", r"/api/settings/(?P<key>[a-z_]+)")
    def set_setting(req, auth, body, key):
        req.require(auth, ("admin",))
        if "value_cents" in body:
            value = int(body["value_cents"])
        elif "value_yuan" in body:
            value = yuan_to_cents(body["value_yuan"])
        else:
            from .errors import bad_request
            raise bad_request("需要 value_cents 或 value_yuan")
        return service.registry.set_setting(key, value, req.actor)

    class ApiRequest:
        def __init__(self, handler, method, path, query):
            self.handler = handler
            self.method = method
            self.path = path
            self.query = query
            self.actor = None

        def require(self, auth, roles):
            if roles is None:
                return
            if auth["role"] not in roles:
                raise forbidden(f"需要角色：{'、'.join(roles)}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SeasonalLaborPay/1.0"

        def _authenticate(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise unauthorized()
            auth = service.registry.authenticate(header[7:].strip())
            if auth is None:
                raise unauthorized()
            return auth

        def _dispatch(self, method):
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._write_json(200, service.health())
                return
            if not parsed.path.startswith("/api/"):
                self._write_json(404, {"error": "not_found", "message": "路由不存在"})
                return
            auth = self._authenticate()
            actor = f"{auth['role']}:{auth['subject']}"
            qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

            body = {}
            if method in ("POST", "PUT"):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ApiError(400, "bad_request", f"请求体不是合法 JSON：{exc}")
                if not isinstance(body, dict):
                    raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")

            for route_method, regex, fn in routes:
                if route_method != method:
                    continue
                match = regex.match(parsed.path)
                if not match:
                    continue
                req = ApiRequest(self, method, parsed.path, qs)
                req.actor = actor
                result = fn(req, auth, body, **match.groupdict())
                self._write_json(200, result)
                return
            self._write_json(404, {"error": "not_found", "message": "路由不存在"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def _handle(self, method):
            try:
                self._dispatch(method)
            except ApiError as exc:
                self._write_json(exc.status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001 - 统一兜底，避免堆栈外泄
                service.db.audit("system", "unhandled_error", "server",
                                 payload={"path": self.path, "error": repr(exc)})
                self._write_json(500, {"error": "internal_error",
                                       "message": "服务内部错误"})
                raise

        def _write_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False,
                              default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    return Handler
