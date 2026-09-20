"""节令用工计酬核验的运行入口。

  python3 service.py --check                 # 自检（含一次端到端计酬演练）
  python3 service.py --port 8000             # 启动 HTTP 服务
  python3 service.py --store data/events.jsonl --port 8000
  python3 service.py --demo --store demo.jsonl  # 写入一套屯昌月饼工坊演示数据

令牌通过环境变量覆盖：ADMIN_TOKEN / LEADER_TOKEN / FINANCE_TOKEN / QC_TOKEN /
SCANNER_TOKEN；村民令牌形如 worker-<worker_id>，由建档接口返回。
"""

from __future__ import annotations

import argparse
import os
from http.server import ThreadingHTTPServer

from api import ROLE_TOKENS, Api, make_handler
from commands import PayService
from events import EventStore
from rules import Rules

SERVICE_ID = "seasonal-labor-pay"
SERVICE_NAME = "节令用工计酬核验"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(store_path: str | None = None) -> PayService:
    store = EventStore(store_path)
    return PayService(store, Rules())


def build_api(service: PayService | None = None) -> Api:
    tokens = {
        role: os.environ.get(f"{role.upper()}_TOKEN", default)
        for role, default in ROLE_TOKENS.items()
    }
    return Api(service or build_service(), tokens, health=health_payload())


def self_check() -> None:
    """无文件副作用的端到端自检。"""
    assert health_payload()["service"] == SERVICE_ID
    service = build_service(None)
    actor = "admin:self-check"
    service.register_worker("w_check", "自检村民", actor)
    service.record_training("w_check", "food_safety", "2026-09-01 09:00", actor)
    service.record_health_cert("w_check", "2027-09-01", actor)
    service.create_operation("op_check", actor, piece_unit_pay=1,
                             required_trainings=["food_safety"])
    service.create_shift("sh_check", "op_check", "2026-09-19 20:00",
                         "2026-09-20 02:00", actor)
    service.assign_worker("sh_check", "w_check", actor)
    service.record_punch("w_check", "in", "2026-09-19 20:00", actor)
    service.record_punch("w_check", "out", "2026-09-20 02:00", actor)
    payroll = service.compute_payroll("2026-09-19", "2026-09-19")
    assert payroll["worker_count"] == 1
    line = payroll["workers"][0]["lines"][0]
    assert line["hours_by_kind"].get("night") == 2.0
    assert service.verify_chain()["ok"]
    print("基础检查通过")


def seed_demo(service: PayService) -> None:
    """写入一套覆盖主要场景的演示数据（幂等：重复运行跳过）。"""
    if service.list_workers():
        print("演示数据已存在，跳过")
        return
    admin, leader, qc, scanner = ("admin:人事", "leader:王班长", "qc:质检员", "scanner:扫码器")
    service.register_worker("w_amei", "阿梅", admin, phone="138****0001")
    service.register_worker("w_alan", "阿兰", admin, phone="138****0002")
    service.register_worker("w_azhu", "阿珠", admin, phone="138****0003")
    for wid in ("w_amei", "w_alan", "w_azhu"):
        service.record_training(wid, "food_safety", "2026-09-01 09:00", admin,
                                course_name="食品安全培训")
        service.record_health_cert(wid, "2027-08-31", admin)
    # 阿珠健康证已过期，演示资质闸门拦截
    service.record_health_cert("w_azhu", "2026-09-10", admin)

    service.create_operation("op_bake", admin, name="烤炉",
                             hourly_wage=24, required_trainings=["food_safety"])
    service.create_operation("op_pack", admin, name="内包装计件",
                             piece_unit_pay=0.5, required_trainings=["food_safety"])
    service.create_operation("op_load", admin, name="装盒",
                             hourly_wage=18, required_trainings=["food_safety"])

    # 白班：阿梅装盒；夜班跨午夜：阿兰内包装计件
    service.create_shift("sh_day_0920", "op_load", "2026-09-20 08:00",
                         "2026-09-20 16:00", leader)
    service.assign_worker("sh_day_0920", "w_amei", leader)
    service.create_shift("sh_night_0920", "op_pack", "2026-09-20 22:00",
                         "2026-09-21 04:00", leader)
    assign_event = service.assign_worker("sh_night_0920", "w_alan", leader)
    night_asg = assign_event["payload"]["assignment_id"]

    # 阿梅：打卡、30 分钟无薪休息、1 小时设备停机保底
    service.record_punch("w_amei", "in", "2026-09-20 08:00", scanner,
                         scanner_id="gate1", client_event_id="demo-amei-in")
    service.record_punch("w_amei", "out", "2026-09-20 16:00", scanner,
                         scanner_id="gate1", client_event_id="demo-amei-out")
    service.record_incident("sh_day_0920", "w_amei", "break",
                            "2026-09-20 12:00", "2026-09-20 12:30", leader,
                            note="午饭休息（无薪）")
    service.record_incident("sh_day_0920", "w_amei", "equipment_down",
                            "2026-09-20 14:00", "2026-09-20 15:00", leader,
                            note="成型机故障停机，按保底计")

    # 阿兰：跨午夜夜班计件
    service.record_punch("w_alan", "in", "2026-09-20 22:00", scanner,
                         client_event_id="demo-alan-in")
    service.record_punch("w_alan", "out", "2026-09-21 04:00", scanner,
                         client_event_id="demo-alan-out")
    service.report_quantity("w_alan", "op_pack", "sh_night_0920", 200, qc,
                            quantity_id="qty_demo_alan_ok")
    service.qc_decide("qty_demo_alan_ok", "accepted", qc, evidence="抽检合格")

    # 临时顶班：00:30–03:30 阿梅替阿兰 3 小时（与其白班不重叠，需另有打卡佐证）
    service.substitute_worker("sh_night_0920", night_asg, "w_amei",
                              "2026-09-21 00:30", "2026-09-21 03:30", leader,
                              reason="temporary_sub")
    service.record_punch("w_amei", "in", "2026-09-21 00:30", scanner,
                         client_event_id="demo-amei-sub-in")
    service.record_punch("w_amei", "out", "2026-09-21 03:30", scanner,
                         client_event_id="demo-amei-sub-out")
    service.report_quantity("w_amei", "op_pack", "sh_night_0920", 150, qc,
                            quantity_id="qty_demo_amei_ok")
    service.qc_decide("qty_demo_amei_ok", "accepted", qc, evidence="抽检合格")

    # 一批质检退回：进入责任复核，不直接扣钱（阿梅夜班里有 30 件包装被退回）
    service.report_quantity("w_amei", "op_pack", "sh_night_0920", 30, qc,
                            quantity_id="qty_demo_reject")
    service.qc_decide("qty_demo_reject", "rejected", qc,
                      evidence="QC照片#IMG-0921-08：封口不牢退回，待责任复核")
    service.open_review("qty_demo_reject", qc)
    print("演示数据写入完成")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="无副作用自检")
    parser.add_argument("--store", help="事件日志 JSONL 路径（默认内存模式，重启即失）")
    parser.add_argument("--demo", action="store_true", help="写入演示数据后退出")
    args = parser.parse_args()

    if args.check:
        self_check()
        return

    service = build_service(args.store)
    if args.demo:
        seed_demo(service)
        return

    api = build_api(service)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(api))
    print(f"{SERVICE_NAME} 已启动：http://0.0.0.0:{args.port}  事件日志: "
          f"{args.store or '内存模式'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
