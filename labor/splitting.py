"""工时拆分规则（当地规则的唯一实现处）。

规则：
1. 工作区间跨午夜时，按日历日拆成多段，夜班工资归属各实际发生日；
2. 区间内的休息中断（用餐、停工休息）从区间中扣除，不计酬；
3. 换岗由班组长按相邻区间分别申报，区间与工序一一对应；
4. 顶班仅改变段上的角色标记（substitute），不改变拆分规则。
"""

from datetime import datetime, timedelta

from .errors import bad_request
from .timeutil import parse_ts

WORK_CATEGORIES = ("training", "regular", "piecework", "rework", "standby")
ALL_CATEGORIES = WORK_CATEGORIES + ("break",)


def _midnights(start, end):
    """生成 start、end 之间的每个午夜时刻。"""
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


def _subtract_breaks(day_start, day_end, breaks):
    """从一个不跨午夜的区间中扣除休息段，返回工作片段列表。"""
    cuts = [(max(b_start, day_start), min(b_end, day_end))
            for b_start, b_end in breaks
            if b_end > day_start and b_start < day_end]
    cuts.sort()
    pieces = []
    cursor = day_start
    for b_start, b_end in cuts:
        if b_start < cursor:
            # 重叠的休息段取并集，避免异常输入把时间"扣成负数"。
            b_start = cursor
        if b_start > cursor:
            pieces.append((cursor, b_start))
        if b_end > cursor:
            cursor = b_end
    if cursor < day_end:
        pieces.append((cursor, day_end))
    return pieces


def split_interval(start_raw, end_raw, breaks_raw=()):
    """把 [start, end] 按午夜和休息拆成 (start, end) 工作片段。

    休息段以 (start, end) 二元组序列给出，可以跨午夜。
    返回片段均不跨午夜、互不重叠、按时间排序，分钟数之和严格等于
    总时长减去休息时长。
    """
    start = parse_ts(start_raw)
    end = parse_ts(end_raw)
    if end <= start:
        raise bad_request("工作区间结束时间必须晚于开始时间")
    breaks = []
    for item in breaks_raw or ():
        b_start = parse_ts(item[0] if isinstance(item, (list, tuple)) else item["start"])
        b_end = parse_ts(item[1] if isinstance(item, (list, tuple)) else item["end"])
        if b_end <= b_start:
            raise bad_request("休息中断结束时间必须晚于开始时间")
        if b_start < start or b_end > end:
            raise bad_request("休息中断必须落在工作区间内")
        breaks.append((b_start, b_end))

    boundaries = [start] + list(_midnights(start, end)) + [end]
    pieces = []
    for i in range(len(boundaries) - 1):
        pieces.extend(_subtract_breaks(boundaries[i], boundaries[i + 1], breaks))
    return pieces


def minutes_between(start, end):
    return int((end - start).total_seconds() // 60)
