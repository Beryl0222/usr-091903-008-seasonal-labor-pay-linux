"""节令用工计酬核验的运行入口。

用法：
  python3 service.py --check                 自检服务身份
  python3 service.py --port 8000 --db labor.db
  python3 service.py --bootstrap-tokens      初始化五个岗位令牌并打印
"""

import argparse
import json
import os
from http.server import ThreadingHTTPServer

from labor.app import Service
from labor.httpapi import create_handler

SERVICE_ID = "seasonal-labor-pay"
SERVICE_NAME = "节令用工计酬核验"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 向后兼容旧契约测试的最小 Handler：仅健康检查。
from http.server import BaseHTTPRequestHandler  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    """提供健康检查（领域接口由 labor 包中的应用 Handler 承载）。"""

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def build_server(port, db_path):
    service = Service(db_path)
    app_handler = create_handler(service)
    server = ThreadingHTTPServer(("0.0.0.0", port), app_handler)
    server.service = service
    return server


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.environ.get("LABOR_DB", "labor.db"),
                        help="SQLite 数据库路径（默认 labor.db）")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bootstrap-tokens", action="store_true",
                        help="初始化 hr/production/leader/finance/admin 令牌")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Service(":memory:").close()
        print("基础检查通过")
        return
    if args.bootstrap_tokens:
        service = Service(args.db)
        tokens = service.registry.bootstrap_staff_tokens()
        print(json.dumps(tokens, ensure_ascii=False, indent=2))
        service.close()
        return
    server = build_server(args.port, args.db)
    try:
        server.serve_forever()
    finally:
        server.service.close()


if __name__ == "__main__":
    main()
