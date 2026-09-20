"""追加式事件日志，带哈希链。

任何业务事实都以事件追加：原始打卡（punch_in/punch_out）一经记录不可修改、
不可删除；更正只能通过补偿事件（punch_correction）表达，原始记录仍在。

每条事件包含：
  seq   单调递增序号
  ts    事件受理时间（服务端）
  type  事件类型
  actor 操作人（角色:标识）
  payload 事件内容
  prev_hash 上一条事件的哈希
  hash  本条哈希（含 prev_hash）

封账（payroll_closed）之后再追加与该批次相关的事件并不被物理禁止
追加式日志不删数据，但会产生"封账后变更"，状态层会把这类事件单独标记并
让校验/对账暴露差异，保证"不可悄然修改"。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime

GENESIS_HASH = "0" * 64


def _canonical(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_hash(prev_hash: str, event_type: str, ts: str, actor: str, payload: dict) -> str:
    body = _canonical({"prev": prev_hash, "type": event_type, "ts": ts, "actor": actor, "payload": payload})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Event:
    __slots__ = ("seq", "ts", "type", "actor", "payload", "prev_hash", "hash")

    def __init__(self, seq, ts, event_type, actor, payload, prev_hash, hash_value):
        self.seq = seq
        self.ts = ts
        self.type = event_type
        self.actor = actor
        self.payload = payload
        self.prev_hash = prev_hash
        self.hash = hash_value

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class EventStore:
    """JSONL 追加存储。文件每行一条事件，外加一行链校验信息。"""

    def __init__(self, path: str | None = None, clock=None):
        self.path = path
        self._clock = clock or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._lock = threading.RLock()
        self._events: list[Event] = []
        if path and os.path.exists(path):
            self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                event = Event(
                    seq=raw["seq"],
                    ts=raw["ts"],
                    event_type=raw["type"],
                    actor=raw["actor"],
                    payload=raw["payload"],
                    prev_hash=raw["prev_hash"],
                    hash_value=raw["hash"],
                )
                self._events.append(event)
        self.verify()

    def _append_line(self, event: Event):
        if not self.path:
            return
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, event_type: str, payload: dict, actor: str) -> Event:
        """追加事件。payload 中若带 client_event_id，重复追加会被拒绝（幂等去重）。"""
        with self._lock:
            client_id = payload.get("client_event_id")
            if client_id is not None:
                for existing in self._events:
                    if existing.payload.get("client_event_id") == client_id:
                        raise DuplicateEventError(client_id, existing.seq)
            ts = self._clock()
            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            seq = len(self._events) + 1
            hash_value = event_hash(prev_hash, event_type, ts, actor, payload)
            event = Event(seq, ts, event_type, actor, dict(payload), prev_hash, hash_value)
            self._append_line(event)
            self._events.append(event)
            return event

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return [event for event in self._events if event.seq > seq]

    def verify(self) -> dict:
        """校验整条哈希链，返回 {ok, events, broken_at}。"""
        prev_hash = GENESIS_HASH
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                return {"ok": False, "events": len(self._events), "broken_at": index, "reason": "序号不连续"}
            if event.prev_hash != prev_hash:
                return {"ok": False, "events": len(self._events), "broken_at": index, "reason": "哈希链断裂"}
            expected = event_hash(event.prev_hash, event.type, event.ts, event.actor, event.payload)
            if event.hash != expected:
                return {"ok": False, "events": len(self._events), "broken_at": index, "reason": "内容哈希不符"}
            prev_hash = event.hash
        return {"ok": True, "events": len(self._events), "broken_at": None, "reason": None}

    @staticmethod
    def verify_disk(path: str) -> dict:
        """从磁盘逐行重读并校验哈希链，用于巡检外部篡改/损坏。

        与内存态不同：不依赖进程内状态，任何被改动、伪造或缺漏的行
        都会被定位（broken_at 给出行号）。
        """
        prev_hash = GENESIS_HASH
        count = 0
        if not os.path.exists(path):
            return {"ok": False, "events": 0, "broken_at": None, "reason": "日志文件不存在"}
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                count += 1
                try:
                    raw = json.loads(line)
                    seq = raw["seq"]
                    if seq != count:
                        return {"ok": False, "events": count, "broken_at": line_no,
                                "reason": f"序号不连续（应为 {count}）"}
                    if raw["prev_hash"] != prev_hash:
                        return {"ok": False, "events": count, "broken_at": line_no,
                                "reason": "哈希链断裂"}
                    expected = event_hash(raw["prev_hash"], raw["type"], raw["ts"],
                                          raw["actor"], raw["payload"])
                    if raw["hash"] != expected:
                        return {"ok": False, "events": count, "broken_at": line_no,
                                "reason": "内容哈希不符（记录可能被篡改）"}
                    prev_hash = raw["hash"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    return {"ok": False, "events": count, "broken_at": line_no,
                            "reason": "事件行损坏或字段缺失"}
        return {"ok": True, "events": count, "broken_at": None, "reason": None}


class DuplicateEventError(Exception):
    """扫码器补传/班组长重复确认时，同一 client_event_id 不得重复计工时。"""

    def __init__(self, client_event_id: str, original_seq: int):
        self.client_event_id = client_event_id
        self.original_seq = original_seq
        super().__init__(f"重复事件已被拒绝: {client_event_id}（原始序号 {original_seq}）")
