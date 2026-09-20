"""时间与金额工具。

全程使用固定格式 ``YYYY-MM-DDTHH:MM:SS`` 的本地时间字符串（工坊不跨时区），
字典序与时间序一致，可直接在 SQLite 中比较。
"""

from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

TS_FMT = "%Y-%m-%dT%H:%M:%S"


def parse_ts(value):
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if not isinstance(value, str) or not value:
        raise ValueError("时间必须是 ISO 字符串")
    text = value.strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        # 兼容仅传日期的情况。
        return datetime.strptime(text, "%Y-%m-%d")


def fmt(dt):
    return dt.strftime(TS_FMT)


def now_ts():
    return fmt(datetime.now().replace(microsecond=0))


def day_of(dt):
    return dt.date().isoformat()


def parse_day(value):
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def yuan_to_cents(value):
    """元（字符串/数字）转整数分。"""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_yuan(cents):
    return f"{Decimal(cents) / 100:.2f}"


def amount_for_minutes(minutes, rate_cents_per_hour):
    """按分钟折算金额（分），四舍五入到分。"""
    if minutes <= 0:
        return 0
    amount = Decimal(minutes) * Decimal(rate_cents_per_hour) / Decimal(60)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
