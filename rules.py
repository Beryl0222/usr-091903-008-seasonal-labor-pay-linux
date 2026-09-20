"""计酬规则与纯计算函数。

所有时间均为 "YYYY-MM-DD HH:MM" 的本地时间（屯昌当地时间，不涉时区换算）。
跨午夜的工作区间会在午夜 00:00 拆分为两个计酬分段。

规则可被工坊参数化（时薪、倍率、停机保底等），默认值体现题述"当地规则"：

- 培训等待：村民已到岗、等待上岗培训/班前培训，按等待时薪计；
- 正常工时：按基本时薪计；
- 计件：按合格件数 × 单件工价，可与工时报酬择高（保证不低于保底）；
- 换岗/临时顶班：按所在岗位的时薪分段计；
- 跨午夜：按日拆分，夜班工时按夜班倍率加成；
- 休息中断：无薪休息段从工时中扣除；
- 设备停机：非工人原因的停机等待按停机保底比例计发；
- 质检退回：不直接扣个人报酬，先进入责任复核；复核确认属个人责任
  的，通过调整项处理，且必须带证据与复核结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

DATE_FMT = "%Y-%m-%d"
TS_FMT = "%Y-%m-%d %H:%M"

# 计酬分段类型
SEG_TRAINING_WAIT = "training_wait"   # 培训等待
SEG_REGULAR = "regular"               # 正常工时
SEG_PIECE = "piece"                   # 计件岗位在岗工时（件数另计）
SEG_NIGHT = "night"                   # 夜班正常工时（跨午夜夜班段）
SEG_BREAK = "break"                   # 休息中断（无薪）
SEG_DOWN = "down"                     # 设备停机保底等待

# 调整项类型
ADJ_REWORK_DEDUCT = "rework_deduct"    # 复核确认个人责任的返工/退回扣减
ADJ_APPEAL_GRANT = "appeal_grant"     # 申诉成立补发
ADJ_OTHER = "other"

QUANTITY_PENDING = "pending"           # 产量待质检
QUANTITY_ACCEPTED = "accepted"         # 质检合格
QUANTITY_REJECTED_PENDING_REVIEW = "rejected_pending_review"  # 退回待复核
QUANTITY_REJECTED_WORKER = "rejected_worker_fault"            # 复核：个人责任
QUANTITY_REJECTED_NONWORKER = "rejected_non_worker"           # 复核：非个人责任


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT)


def fmt_ts(value: datetime) -> str:
    return value.strftime(TS_FMT)


def parse_date(value: str) -> date:
    return datetime.strptime(value, DATE_FMT).date()


def today(value: str | None = None) -> date:
    return parse_date(value) if value else date.today()


def hours_between(start: datetime, end: datetime) -> float:
    """小时数，保留两位（四舍五入到分钟，避免浮点尾差）。"""
    minutes = round((end - start).total_seconds() / 60)
    if minutes < 0:
        raise ValueError("结束时间早于开始时间")
    return round(minutes / 60, 2)


@dataclass
class Rules:
    """工坊当地计酬规则。金额单位：元。"""

    base_hourly_wage: float = 20.0          # 基本时薪
    training_wait_rate: float = 0.5         # 培训等待 = 基本时薪 × 比例
    down_time_guarantee_rate: float = 0.7   # 设备停机保底 = 基本时薪 × 比例
    night_shift_multiplier: float = 1.3     # 夜班（00:00–06:00 部分）倍率
    piece_guarantee: bool = True            # 计件岗位不低于工时保底
    night_start_hour: int = 0               # 夜班时段起（含），本地小时
    night_end_hour: int = 6                 # 夜班时段止（不含）
    health_cert_valid_days: int = 365       # 健康证有效期（天）
    shift_max_hours: float = 12.0           # 单个班次最长工时（校验用）

    def wage_for_segment(self, kind: str, hours: float, hourly_wage: float | None = None) -> float:
        wage = hourly_wage or self.base_hourly_wage
        if kind == SEG_REGULAR or kind == SEG_PIECE:
            rate = 1.0
        elif kind == SEG_NIGHT:
            rate = self.night_shift_multiplier
        elif kind == SEG_TRAINING_WAIT:
            rate = self.training_wait_rate
        elif kind == SEG_DOWN:
            rate = self.down_time_guarantee_rate
        elif kind == SEG_BREAK:
            rate = 0.0
        else:
            raise ValueError(f"未知分段类型: {kind}")
        return round(hours * wage * rate, 2)


@dataclass
class Interval:
    """半开工作区间 [start, end)，kind 决定计酬方式。

    hourly_wage 为该段适用时薪（换岗、临时顶班时各岗位不同）；
    为空则使用规则中的基本时薪。
    """

    start: str
    end: str
    kind: str = SEG_REGULAR
    hourly_wage: float | None = None
    meta: dict = field(default_factory=dict)

    def with_kind(self, kind: str) -> "Interval":
        return Interval(self.start, self.end, kind, self.hourly_wage, dict(self.meta))


def split_at_midnight(interval: Interval) -> list[Interval]:
    """跨午夜区间在每个 00:00 处拆分。夜班时段（默认 00:00–06:00）标为 night。"""
    start = parse_ts(interval.start)
    end = parse_ts(interval.end)
    if end <= start:
        raise ValueError("工作区间结束时间必须晚于开始时间")
    pieces: list[Interval] = []
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
        boundary = min(end, next_midnight)
        pieces.append(
            Interval(fmt_ts(cursor), fmt_ts(boundary), interval.kind, interval.hourly_wage, dict(interval.meta))
        )
        cursor = boundary
    return pieces


def split_by_night(interval: Interval, rules: Rules) -> list[Interval]:
    """把单个不跨午夜区间按夜班时段再切分。"""
    start = parse_ts(interval.start)
    end = parse_ts(interval.end)
    if start.date() != end.date() or end == start:
        return [interval]
    night_start = start.replace(hour=rules.night_start_hour, minute=0)
    night_end = start.replace(hour=rules.night_end_hour, minute=0)
    if not (start < night_end and end > night_start):
        return [interval]
    pieces: list[Interval] = []
    cursor = start
    points = sorted({start, end, max(start, night_start), min(end, night_end)})
    for left, right in zip(points, points[1:]):
        if right <= left:
            continue
        mid = left + (right - left) / 2
        kind = interval.kind
        if interval.kind in (SEG_REGULAR, SEG_PIECE) and night_start <= mid < night_end:
            kind = SEG_NIGHT
        pieces.append(
            Interval(fmt_ts(left), fmt_ts(right), kind, interval.hourly_wage, dict(interval.meta))
        )
    return pieces


def normalize_intervals(intervals: list[Interval], rules: Rules) -> list[Interval]:
    """跨午夜拆分 + 夜班标注 + 重叠区间校验。返回按开始时间排序的分段。"""
    result: list[Interval] = []
    for interval in intervals:
        for piece in split_at_midnight(interval):
            result.extend(split_by_night(piece, rules))
    result.sort(key=lambda item: (item.start, item.end))
    for prev, cur in zip(result, result[1:]):
        if cur.start < prev.end and cur.kind != SEG_BREAK and prev.kind != SEG_BREAK:
            raise ValueError(f"工作时间重叠: {prev.start}–{prev.end} 与 {cur.start}–{cur.end}")
    return result


def subtract_breaks(segments: list[Interval], breaks: list[Interval]) -> list[Interval]:
    """从工作分段中扣除无薪休息（休息中断不产生工时）。"""
    if not breaks:
        return segments
    norm_breaks = normalize_intervals(
        [Interval(b.start, b.end, SEG_BREAK) for b in breaks], Rules()
    )
    work = [s for s in segments if s.kind != SEG_BREAK]
    for br in norm_breaks:
        remaining: list[Interval] = []
        for seg in work:
            if br.end <= seg.start or br.start >= seg.end:
                remaining.append(seg)
                continue
            if br.start <= seg.start and br.end >= seg.end:
                continue  # 整段落在休息内
            if br.start > seg.start:
                remaining.append(Interval(seg.start, br.start, seg.kind, seg.hourly_wage, dict(seg.meta)))
            if br.end < seg.end:
                remaining.append(Interval(br.end, seg.end, seg.kind, seg.hourly_wage, dict(seg.meta)))
        work = remaining
    work.sort(key=lambda item: (item.start, item.end))
    return work


@dataclass
class PieceRecord:
    """一次计件产量上报。退回件进入复核，不直接扣钱。"""

    id: str
    operation_id: str
    quantity: int
    unit_pay: float                 # 单件工价
    status: str = QUANTITY_PENDING
    evidence: str = ""              # 质检退回证据（照片/单号等）
    review_note: str = ""           # 责任复核结论
    accepted_qty: int = 0           # 复核后合格件数
    paid: bool = False              # 是否已计入封账批次

    def payable_quantity(self) -> int:
        if self.status == QUANTITY_ACCEPTED:
            return self.quantity
        if self.status == QUANTITY_REJECTED_PENDING_REVIEW:
            # 质检退回不得直接扣个人报酬：责任复核结论出来前按原额暂计
            return self.quantity
        if self.status == QUANTITY_REJECTED_NONWORKER:
            # 非个人责任（原料/设备等）：计件报酬按原产量照付
            return self.quantity
        if self.status == QUANTITY_REJECTED_WORKER:
            return self.accepted_qty
        return 0  # 尚未质检的产量先挂账，不计入

    def payable_amount(self) -> float:
        return round(self.payable_quantity() * self.unit_pay, 2)


@dataclass
class Adjustment:
    """计酬调整项。复核扣减必须带证据；申诉补发由审批产生。"""

    id: str
    kind: str
    amount: float
    reason: str
    evidence: str = ""
    review_id: str = ""


@dataclass
class PayrollLine:
    """一个人一天的逐项计酬结果（村民可逐项核对）。"""

    worker_id: str
    work_date: str
    segments: list[dict] = field(default_factory=list)
    pieces: list[dict] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    hours_by_kind: dict = field(default_factory=dict)
    piece_amount: float = 0.0
    time_amount: float = 0.0
    guaranteed_amount: float = 0.0
    gross_amount: float = 0.0

    def as_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "work_date": self.work_date,
            "segments": self.segments,
            "pieces": self.pieces,
            "adjustments": self.adjustments,
            "hours_by_kind": self.hours_by_kind,
            "piece_amount": self.piece_amount,
            "time_amount": self.time_amount,
            "guaranteed_amount": self.guaranteed_amount,
            "gross_amount": self.gross_amount,
        }


def build_daily_line(
    worker_id: str,
    work_date: str,
    work_intervals: list[Interval],
    breaks: list[Interval],
    pieces: list[PieceRecord],
    adjustments: list[Adjustment],
    rules: Rules,
    hourly_wage: float | None = None,
) -> PayrollLine:
    """把一个人一天的打卡工作区间、休息、计件与调整汇总为逐项计酬行。"""
    segments = subtract_breaks(normalize_intervals(work_intervals, rules), breaks)
    segment_rows: list[dict] = []
    hours_by_kind: dict = {}
    time_amount = 0.0
    piece_time_amount = 0.0
    for seg in segments:
        hours = hours_between(parse_ts(seg.start), parse_ts(seg.end))
        amount = rules.wage_for_segment(seg.kind, hours, seg.hourly_wage or hourly_wage)
        is_piece_op = bool(seg.meta.get("piece"))
        segment_rows.append(
            {
                "start": seg.start,
                "end": seg.end,
                "kind": seg.kind,
                "hours": hours,
                "hourly_wage": seg.hourly_wage or hourly_wage or rules.base_hourly_wage,
                "piece_operation": is_piece_op,
                "amount": amount,
                "assignment_id": seg.meta.get("assignment_id"),
                "shift_id": seg.meta.get("shift_id"),
                "operation_id": seg.meta.get("operation_id"),
            }
        )
        hours_by_kind[seg.kind] = round(hours_by_kind.get(seg.kind, 0) + hours, 2)
        time_amount = round(time_amount + amount, 2)
        if is_piece_op:
            piece_time_amount = round(piece_time_amount + amount, 2)
    other_time_amount = round(time_amount - piece_time_amount, 2)

    piece_rows: list[dict] = []
    piece_amount = 0.0
    for piece in pieces:
        amount = piece.payable_amount()
        piece_rows.append(
            {
                "piece_id": piece.id,
                "operation_id": piece.operation_id,
                "quantity": piece.quantity,
                "status": piece.status,
                "payable_quantity": piece.payable_quantity(),
                "unit_pay": piece.unit_pay,
                "amount": amount,
            }
        )
        piece_amount = round(piece_amount + amount, 2)

    # 计件岗位：计件段工时报酬与计件报酬择高，保证计件工作不低于保底；
    # 非计件段（培训等待等）照常计酬。
    if pieces and rules.piece_guarantee:
        piece_gross = max(piece_time_amount, piece_amount)
        gross_before_adj = round(other_time_amount + piece_gross, 2)
        guaranteed_amount = piece_time_amount
    else:
        gross_before_adj = round(time_amount + piece_amount, 2)
        guaranteed_amount = 0.0

    adjustment_rows = [
        {
            "adjustment_id": adj.id,
            "kind": adj.kind,
            "amount": adj.amount,
            "reason": adj.reason,
            "evidence": adj.evidence,
            "review_id": adj.review_id,
        }
        for adj in adjustments
    ]
    gross = round(gross_before_adj + sum(adj.amount for adj in adjustments), 2)

    return PayrollLine(
        worker_id=worker_id,
        work_date=work_date,
        segments=segment_rows,
        pieces=piece_rows,
        adjustments=adjustment_rows,
        hours_by_kind=hours_by_kind,
        piece_amount=piece_amount,
        time_amount=time_amount,
        guaranteed_amount=guaranteed_amount,
        gross_amount=gross,
    )
