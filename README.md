# 节令用工计酬核验

中秋月饼工坊季节性用工的计酬后端。人事与生产先核验**食品安全培训、可操作工序授权、健康证明有效期**，再允许班组长把村民编入班次；白班/夜班交叉、换岗顶班、跨午夜工作与休息中断按规则拆分计酬；原始打卡只增不改不删；质检退回进入有证据的责任复核而非直接扣款；村民可查看逐项计算与申诉进展；财务封账后得到哈希链保护、不可悄然修改的支付批次；管理者可核对季节性岗位实际带动的就业人数、有效工时与已付金额。

仅依赖 Python 3 标准库 + SQLite，无第三方包。

## 运行

```bash
python3 service.py --check                      # 自检
python3 service.py --bootstrap-tokens --db labor.db   # 生成五个岗位令牌
python3 service.py --port 8000 --db labor.db          # 启动服务
curl http://127.0.0.1:8000/health
```

岗位令牌角色：`hr`（人事）、`production`（生产/质检）、`leader`（班组长）、`finance`（财务）、`admin`；
创建村民时默认同时签发该村民的个人令牌（只能访问本人数据）。所有 `/api/*` 接口使用
`Authorization: Bearer <token>` 鉴权。

## 核心规则与落地方式

| 争议点 | 规则 | 实现 |
| --- | --- | --- |
| 无资质上岗 | 编入班次前必须同时通过培训、工序授权、健康证核验（按开工日判定有效期） | `POST /api/shifts/{id}/assignments` 返回 422 并留存核验快照 |
| 培训等待 / 正常工时 / 计件 / 设备停机保底 / 返工 | 五类工时分别计价 | 出勤确认单按 `category` 拆分薪酬项；培训补贴、停机保底标准可在 settings 配置 |
| 跨午夜工作 | 按日历日拆段，夜班归属各实际发生日 | 午夜自动切段 |
| 休息中断 | 休息时长不计酬，休息段可跨午夜 | 确认单携带 `breaks`，重叠休息自动并集 |
| 换岗、临时顶班 | 换岗按相邻确认单分别申报；顶班在班次与薪酬项上标记 `substitute` | 顶班须指定被顶替人 |
| 扫码器离线补传 | 不重复计工时 | `client_event_id` 幂等 + 人/设备/方向/时间/来源指纹双重去重 |
| 班组长重复确认 | 不增加工时 | `confirm_key` 幂等，重复提交返回首次结果并标注 `duplicate_confirmation`；同一原始打卡不能锚定两张有效确认单 |
| 纠正历史 | 原始打卡与旧计算永远保留 | 新确认单携带 `supersedes`：旧段标记被取代、旧薪酬项冲销留痕，绝不删除；已封账禁止重算 |
| 质检退回 | 不直接扣个人报酬，进入有证据的责任复核 | 立案时把计件项拆为"合格数量（有效）/争议数量（暂缓）"，原项留痕；复核决定：责任成立→独立负向红冲项，不成立→恢复支付，返工→复验合格后恢复 |
| 村民知情 | 可看逐项计算、质检进展、申诉进展 | `GET /api/workers/{id}/payroll|segments|punches|quality-cases|appeals` |
| 申诉 | 提交→受理→解决/驳回，全程事件流 | 每次流转写入不可变事件；人工补发/核减以独立调整项入账 |
| 财务封账 | 封账后不可悄然修改 | 数据库触发器禁止修改/删除已封账薪酬项、支付行、批次、原始打卡；批次与批次间以 SHA-256 哈希链串联，`GET /api/payment-batches/verify-chain` 可独立重算核对 |
| 就业统计 | 人数、有效工时（按类别）、已付金额 | `GET /api/stats/employment?from=...&to=...`（统计仅计未被取代的有效出勤段） |

## 主要接口

- 人员与资质：`POST /api/workers`、`POST /api/workers/{id}/trainings`、`.../health-certificates`、`.../operations/{op}/grant`、`GET /api/workers/{id}/eligibility`
- 工序：`POST /api/operations`、`GET /api/operations`
- 班次：`POST /api/shifts`、`POST /api/shifts/{id}/assignments`、`GET /api/shifts`
- 打卡：`POST /api/punches`（支持 `source=offline_sync`、`client_event_id`）
- 出勤确认：`POST /api/attendance-reports`（`confirm_key`、`category`、`breaks`、`supersedes`）
- 计件与质检：`POST /api/production-records`、`POST /api/quality-cases`、`POST /api/quality-cases/{id}/decision`（`upheld|rejected|rework`）、`.../rework-complete`
- 村民视图：`GET /api/workers/{id}/payroll`（含逐项金额、批次、案件、申诉状态）
- 申诉：`POST /api/workers/{id}/appeals`、`POST /api/appeals/{id}/accept|resolve|reject`
- 调整：`POST /api/workers/{id}/pay-adjustments`（财务/人事，正负均可，必须填说明）
- 封账：`POST /api/payment-batches/seal`、`GET /api/payment-batches`、`GET /api/payment-batches/verify-chain`
- 参数：`PUT /api/settings/{key}`（如 `training_allowance_cents_per_hour`、`standby_guarantee_cents_per_hour`）

## 金额口径

所有金额以整数「分」存储，接口同时返回 `*_yuan` 文本；分钟折算四舍五入到分。
封账只纳入状态为 `active` 的薪酬项；`withheld`（质检争议暂缓）不支付，`reversed` 为留痕冲销，
负向项（质检核减、人工调整）可与正常项同批封账。

## 测试

```bash
npm test          # 等价于 python3 -m unittest -v service_contract test_labor
```

`service_contract.py` 为原基础服务契约；`test_labor.py` 为 17 个端到端用例，
覆盖资质闸门、跨午夜/休息拆分、培训/保底分类、离线补传与重复确认幂等、
同一打卡防重、重算留痕、顶班、质检三种复核结论、申诉进展、封账防篡改与哈希链、
就业统计、人工调整留痕、角色鉴权与本人数据隔离。
