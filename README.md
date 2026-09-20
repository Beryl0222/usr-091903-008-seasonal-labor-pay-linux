# 节令用工计酬核验（屯昌月饼工坊）

中秋旺季临时工的用工计酬后端。把"培训等待 / 正常工时 / 计件返工 / 设备停机保底"
拆成可逐项核对的计酬分段，先核验资质再编入班次，原始打卡永久保留，质检退回进入
有证据的责任复核而非直接扣钱，村民可查看自己的逐项计算与申诉进展，财务得到
封账后不可悄然修改的支付批次，管理者可核对季节性岗位的就业人数、有效工时与已付金额。

## 运行与测试

```bash
python3 service.py --check                 # 无副作用自检
python3 service.py --port 8000             # 启动 HTTP 服务（默认内存事件日志）
python3 service.py --store data/log.jsonl --port 8000   # 落盘持久化
python3 service.py --demo --store demo.jsonl            # 写入一套演示数据

npm test                                   # 运行全部契约与端到端测试
```

启动后 `GET /health` 返回服务身份。令牌可用环境变量覆盖：
`ADMIN_TOKEN / LEADER_TOKEN / FINANCE_TOKEN / QC_TOKEN / SCANNER_TOKEN`
（默认开发值见 `api.py`，生产必须覆盖）。村民令牌为 `worker-<worker_id>`，
由建档接口返回，只能访问本人数据。

## 业务流程

```
建档（培训记录 + 健康证）
        │  资质核验：食品安全培训齐全且未过期、健康证在有效期、可操作工序匹配
        ▼
班组长建班次、把人编入班次 ── 换岗/临时顶班（窗口内由替岗者计酬，原岗位者扣除）
        ▼
扫码器打卡（白班/夜班交叉；支持离线补传、班组长代确认，均幂等去重）
        │  设备停机登记、无薪休息登记、培训等待登记
        ▼
按规则拆分：跨午夜按日拆、00:00–06:00 标夜班倍率、休息扣工时、停机按保底、
等待按等待薪；在岗工时 = 编排窗口 ∩ 打卡区间（打卡是在岗证据）
        ▼
计件产量上报 → 质检合格计酬 / 退回（必须带证据）→ 开启责任复核
        │  复核期间按原额暂计，不直接扣个人报酬
        ▼
结论：个人责任→按合格件数计酬；非个人责任（原料/设备）→原额照付；
若已封账，差额以带证据的负向调整项进入下一批次，旧批次不变
        ▼
村民查看逐项计算/原始打卡/复核与申诉进展 → 可发起申诉 → 管理方处理（成立则带证据补发）
        ▼
财务按连续日期封账生成支付批次（逐项明细 + 总额哈希 + 封账时点事件序号）
        ▼
管理者查看季节岗位统计：就业人数、人次、有效工时（分类）、已付金额
```

## 计酬规则（`rules.py`，可参数化）

| 分段 | 口径 | 默认 |
|---|---|---|
| 正常工时 `regular` | 基本时薪（可用工序时薪覆盖） | 20 元/时 |
| 计件在岗 `piece` | 同正常工时，作为计件保底 | 同上 |
| 夜班 `night` | 00:00–06:00 部分按倍率 | ×1.3 |
| 培训等待 `training_wait` | 已到岗打卡、等待上岗/班前培训 | 基本时薪 ×0.5 |
| 设备停机 `down` | 非工人原因停机、有打卡佐证 | 基本时薪 ×0.7 |
| 休息中断 `break` | 无薪，从工时扣除 | 0 |

- 计件岗位：当日"计件段工时保底"与"合格件计件额"**择高**，保证不低于保底；
  等待等非计件段照常计酬，不卷入择高。
- 质检未完成：计件额挂账（工时保底照付），封账前必须完成质检。
- 退回待复核：按原额**暂计**并在批次中列明 `provisional_items`；封账默认拦截，
  可显式 `allow_provisional` 放行。
- 时间按分钟取整、金额保留两位小数。

## 不可篡改与可复核

- 所有事实都是追加事件（`events.py`），每条含前一条哈希，形成 SHA-256 哈希链；
  JSONL 落盘并 `fsync`。**原始打卡不可修改、不可删除**；更正只追加
  `punch_corrected`，原始记录保留并被标记。
- 支付批次冻结逐项明细、总额哈希与封账时点事件序号。`GET .../verify` 会重放至
  封账事件前一条重新计算，逐行比对并列出封账后的关联事件（复核结论、调整项等），
  旧批次永远保持原值。
- `GET /admin/events/verify?disk=1` 从磁盘逐行重读校验，可定位被篡改/损坏的行号。
- 扫码器离线补传：同一 `client_event_id` 幂等拒绝；同人同向同刻打卡按内容去重；
  班组长重复确认同样拒绝——均不增加工时。

## 主要接口（`Authorization: Bearer <token>`）

| 角色 | 接口示例 |
|---|---|
| 人事 admin | `POST /admin/workers`、`.../training`、`.../health-cert`、`POST /admin/operations`、`GET /admin/stats/seasonal`、`POST /admin/appeals/{id}/resolve` |
| 班组长 leader | `POST /leader/shifts`、`GET /leader/eligibility`、`POST /leader/shifts/{id}/assign`、`.../substitute`、`POST /leader/incidents` |
| 扫码器 scanner | `POST /scanner/punches`（支持 `source=offline_upload`） |
| 质检 qc | `POST /qc/quantities`、`.../decision`、`POST /qc/reviews`、`.../conclude` |
| 村民 worker | `GET /workers/{id}/payroll?period_start=&period_end=`、`POST /workers/{id}/appeals` |
| 财务 finance | `GET /payroll/compute`、`POST /finance/payroll/close`、`GET /finance/payroll/batches`、`.../{id}/verify`、`POST /finance/adjustments` |

错误码：400 业务规则拒绝、401 未认证、403 越权（含村民访问他人数据）、
404 不存在、409 重复/冲突。

## 模块结构

- `rules.py`：规则参数与纯计算（拆分、保底、逐行汇总），无副作用。
- `events.py`：追加式哈希链事件存储与磁盘校验。
- `state.py`：事件回放得到的只读快照（含按序号重放，供批次复核）。
- `commands.py`：领域命令（资质、编排、打卡、中断、计件、复核、申诉、封账、统计）。
- `api.py`：角色鉴权与 JSON 路由。
- `service.py`：运行入口、自检、演示数据。
- `test_payroll.py` / `service_contract.py`：端到端与契约测试。

## 明确的边界（有意为之）

- 等待/停机报酬必须有打卡佐证；无佐证只告警不计酬，交班组长核对。
- 缺少上班/下班配对的卡不计工时且阻断封账，须走"打卡更正"（原始卡保留）补齐；
  有打卡却未编入班次的时段只告警不计酬，防止静默少薪也防止虚增工时。
- 已封账日期不能直接改打卡/补报产量，只能走复核或申诉留痕。
- 批次必须按日期连续封账，防止漏封、重封。
- 村民令牌是演示级方案（`worker-<id>`）；生产应换发不可猜测的随机令牌并对接实名。
- 健康证/培训有效期按自然日判断；规则中的倍率与时长均为当地默认值，可在
  `Rules` 中调整后重算历史（未封账部分）。
