"""SQLite 数据访问。

关键不可变表（``punch_events``、``attendance_segments``、``pay_items``、
``payments`` 等）通过触发器禁止 UPDATE/DELETE，封账后的批次另加行级守卫，
满足"原始打卡永远保留""封账后不可悄然修改"。
"""

import hashlib
import json
import sqlite3
import threading

from .timeutil import now_ts

IMMUTABLE_TABLES = (
    "punch_events",
    "attribution_links",
    "quality_case_events",
    "payment_batches",
    "payments",
    "appeal_events",
    "audit_log",
)


def _immutable_triggers(table):
    return (
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON {table} BEGIN\n"
        f"  SELECT RAISE(ABORT, '{table} 为不可变记录，不允许修改');\n"
        f"END;\n"
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete BEFORE DELETE ON {table} BEGIN\n"
        f"  SELECT RAISE(ABORT, '{table} 为不可变记录，不允许删除');\n"
        f"END;\n"
    )


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 人员（村民/临时工）
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    id_card TEXT,
    phone TEXT,
    created_at TEXT NOT NULL,
    terminated_at TEXT
);

-- 资质：食品安全培训
CREATE TABLE IF NOT EXISTS food_safety_trainings (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    trained_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    evidence TEXT
);

-- 资质：健康证明
CREATE TABLE IF NOT EXISTS health_certificates (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    issued_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    certificate_no TEXT,
    evidence TEXT
);

-- 工序定义
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    hourly_rate_cents INTEGER NOT NULL,        -- 正常工时工资标准（分/小时）
    piece_rate_cents INTEGER NOT NULL DEFAULT 0, -- 计件单价（分/件）
    requires_food_safety INTEGER NOT NULL DEFAULT 1
);

-- 可操作工序授权（带核验快照）
CREATE TABLE IF NOT EXISTS worker_operations (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    UNIQUE(worker_id, operation_id)
);

-- 班次（排班）
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    work_date TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,             -- 可跨午夜
    created_at TEXT NOT NULL,
    created_by TEXT,
    locked INTEGER NOT NULL DEFAULT 0      -- 已纳入封账批次后置 1
);
CREATE TABLE IF NOT EXISTS shift_assignments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    role TEXT NOT NULL DEFAULT 'normal',   -- normal | substitute（顶班）
    replaced_worker_id INTEGER REFERENCES workers(id),
    assigned_at TEXT NOT NULL,
    assigned_by TEXT,
    eligibility_snapshot TEXT NOT NULL,    -- 编入时的三项资质快照
    UNIQUE(shift_id, worker_id)
);

-- 原始打卡事件（只增不改不删）
CREATE TABLE IF NOT EXISTS punch_events (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    scan_code TEXT,                        -- 扫码器编号
    event_type TEXT NOT NULL,              -- in | out
    event_time TEXT NOT NULL,              -- 设备实际时间
    received_at TEXT NOT NULL,             -- 服务端接收时间
    source TEXT NOT NULL DEFAULT 'online', -- online | offline_sync
    client_event_id TEXT,                  -- 扫码器/班组长客户端幂等键
    confirmed_by TEXT,                     -- 班组长确认令牌
    confirm_seq INTEGER,                   -- 同一确认动作的序号
    created_at TEXT NOT NULL
);
-- 同一物理事件只接受一次：客户端幂等键，或 人+设备+方向+时间+来源 的指纹
CREATE UNIQUE INDEX IF NOT EXISTS ux_punch_client ON punch_events(client_event_id)
    WHERE client_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_punch_fingerprint
    ON punch_events(worker_id, scan_code, event_type, event_time, source);

-- 原始事件归并到出勤区间的链接（同一原始事件可被重算时多次引用？不：重算不改老数据，
-- 而是新版本段——这里每条链接对应唯一物理事件的一次采用）
CREATE TABLE IF NOT EXISTS attribution_links (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL,
    punch_event_id INTEGER NOT NULL REFERENCES punch_events(id),
    role TEXT NOT NULL,                    -- start | end
    UNIQUE(punch_event_id, segment_id)     -- 同一版本段中端点唯一；重算产生新版本段允许复用
);

-- 拆分后的出勤段（只增；重算则作废旧段——通过 superseded_by 标记而非删除）
CREATE TABLE IF NOT EXISTS attendance_segments (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    shift_id INTEGER REFERENCES shifts(id),
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    segment_date TEXT NOT NULL,           -- 日历日（跨午夜被拆开）
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    category TEXT NOT NULL,               -- training | regular | piecework | standby | rework | break（break 不计酬）
    role TEXT NOT NULL DEFAULT 'normal',  -- normal | substitute
    payable INTEGER NOT NULL DEFAULT 1,
    report_group TEXT NOT NULL,           -- 同一次班组长确认拆出的多段共享一个组号
    supersedes_group TEXT,                -- 重算时所取代的旧报告组
    version INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    CHECK (ended_at > started_at),
    CHECK (category IN ('training','regular','piecework','standby','rework','break'))
);

-- 计件产量（班组长录入）
CREATE TABLE IF NOT EXISTS production_records (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    segment_id INTEGER REFERENCES attendance_segments(id),
    quantity INTEGER NOT NULL,
    work_date TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT,
    client_event_id TEXT,
    UNIQUE(worker_id, operation_id, segment_id, client_event_id)
);

-- 质检退回案件：产量退回不直接扣个人报酬，进入责任复核
CREATE TABLE IF NOT EXISTS quality_cases (
    id INTEGER PRIMARY KEY,
    production_record_id INTEGER NOT NULL REFERENCES production_records(id),
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    quantity INTEGER NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | upheld | rejected | rework
    withheld_cents INTEGER NOT NULL DEFAULT 0, -- 争议期间暂缓的金额（仍属于待复核，不消失）
    opened_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT
    CHECK (status IN ('open','upheld','rejected','rework'))
);
CREATE TABLE IF NOT EXISTS quality_case_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES quality_cases(id),
    event_type TEXT NOT NULL,             -- open | decide | rework_done | note
    payload TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

-- 薪酬明细项（逐段/逐件；只增。作废以 reversed_by 标记，款项不删）
CREATE TABLE IF NOT EXISTS pay_items (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    work_date TEXT NOT NULL,
    category TEXT NOT NULL,
    ref_type TEXT NOT NULL,               -- segment | production | quality_adjust
    ref_id INTEGER NOT NULL,
    minutes INTEGER,
    quantity INTEGER,
    unit_rate_cents INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active | withheld | reversed
    withheld_reason TEXT,
    quality_case_id INTEGER REFERENCES quality_cases(id),
    batch_id INTEGER,                      -- 封账后写入
    version INTEGER NOT NULL DEFAULT 1,
    supersedes INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payitems_worker_date ON pay_items(worker_id, work_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payitem_ref ON pay_items(ref_type, ref_id, version);

-- 支付批次（封账后冻结）
CREATE TABLE IF NOT EXISTS payment_batches (
    id INTEGER PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sealed',  -- 封账即冻结
    total_cents INTEGER NOT NULL,
    worker_count INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    prev_hash TEXT,
    row_hash TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    sealed_by TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES payment_batches(id),
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    amount_cents INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    row_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, worker_id)
);

-- 申诉（村民可见进展）
CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    category TEXT NOT NULL,              -- hours | quality | pay | other
    pay_item_id INTEGER REFERENCES pay_items(id),
    reason TEXT NOT NULL,
    evidence TEXT,
    status TEXT NOT NULL DEFAULT 'submitted', -- submitted | reviewing | resolved | rejected
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
    CHECK (status IN ('submitted','reviewing','resolved','rejected'))
);
CREATE TABLE IF NOT EXISTS appeal_events (
    id INTEGER PRIMARY KEY,
    appeal_id INTEGER NOT NULL REFERENCES appeals(id),
    event_type TEXT NOT NULL,            -- submit | accept | resolve | reject | note
    message TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

-- 审计日志
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    payload TEXT,
    created_at TEXT NOT NULL
);

-- 访问令牌（简单内置鉴权）
CREATE TABLE IF NOT EXISTS access_tokens (
    token TEXT PRIMARY KEY,
    subject TEXT NOT NULL,               -- 账号/村民标识
    role TEXT NOT NULL,                  -- hr | production | leader | finance | admin | worker
    worker_id INTEGER REFERENCES workers(id)
);

-- 键值表（幂等键缓存等）
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    response_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 系统参数（保底标准、培训补贴等，以"分/小时"计）
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""


class Database:
    def __init__(self, path=":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = self._connect()
        self.init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @property
    def conn(self):
        return self._conn

    def init_schema(self):
        with self._lock:
            conn = self.conn
            conn.executescript(SCHEMA)
            for table in IMMUTABLE_TABLES:
                conn.executescript(_immutable_triggers(table))
            # 出勤段：允许在重算时写入 superseded_by 指针（旧段留痕），
            # 但时间、分类、分钟数等事实字段一旦写入不可改。
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_attendance_segments_guard
                BEFORE UPDATE ON attendance_segments
                WHEN NEW.started_at != OLD.started_at OR NEW.ended_at != OLD.ended_at
                  OR NEW.minutes != OLD.minutes OR NEW.category != OLD.category
                  OR NEW.operation_id != OLD.operation_id OR NEW.shift_id IS NOT OLD.shift_id
                  OR NEW.segment_date != OLD.segment_date
                  OR NEW.payable != OLD.payable OR NEW.role != OLD.role
                  OR NEW.report_group != OLD.report_group OR NEW.worker_id != OLD.worker_id
                BEGIN
                  SELECT RAISE(ABORT, '出勤段的事实字段不可修改，仅允许以新版本段取代');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_attendance_segments_no_delete
                BEFORE DELETE ON attendance_segments
                BEGIN
                  SELECT RAISE(ABORT, '出勤段不允许删除，仅允许以新版本段取代');
                END;
                -- 薪酬项：允许状态流转（暂缓/恢复/冲销）与批次归属，
                -- 但已计算的金额、数量、费率不可悄然改动；冲销必须新建红冲项。
                CREATE TRIGGER IF NOT EXISTS trg_pay_items_guard
                BEFORE UPDATE ON pay_items
                WHEN NEW.amount_cents != OLD.amount_cents
                  OR NEW.unit_rate_cents != OLD.unit_rate_cents
                  OR NEW.minutes != OLD.minutes OR NEW.quantity != OLD.quantity
                  OR NEW.ref_type != OLD.ref_type OR NEW.ref_id != OLD.ref_id
                  OR NEW.work_date != OLD.work_date OR NEW.worker_id != OLD.worker_id
                  OR NEW.category != OLD.category
                BEGIN
                  SELECT RAISE(ABORT, '薪酬项的计算结果不可修改，冲销请新建红冲项');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_pay_items_no_delete
                BEFORE DELETE ON pay_items
                BEGIN
                  SELECT RAISE(ABORT, '薪酬项不允许删除，冲销请新建红冲项');
                END;
                -- 已封账的薪酬项任何字段都不得再动。
                CREATE TRIGGER IF NOT EXISTS trg_pay_items_sealed_guard
                BEFORE UPDATE ON pay_items
                WHEN OLD.batch_id IS NOT NULL
                BEGIN
                  SELECT RAISE(ABORT, '薪酬项已随批次封账，不可修改');
                END;
                -- 申诉：只允许状态与更新时间变化，事由、归属等不可改；进展靠事件流。
                CREATE TRIGGER IF NOT EXISTS trg_appeals_guard
                BEFORE UPDATE ON appeals
                WHEN NEW.worker_id != OLD.worker_id OR NEW.category != OLD.category
                  OR NEW.pay_item_id IS NOT OLD.pay_item_id OR NEW.reason != OLD.reason
                  OR NEW.created_at != OLD.created_at
                BEGIN
                  SELECT RAISE(ABORT, '申诉的事实内容不可修改，仅允许状态流转');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_appeals_no_delete
                BEFORE DELETE ON appeals
                BEGIN
                  SELECT RAISE(ABORT, '申诉记录不允许删除');
                END;
                -- 质量案件：只允许状态与决定字段流转，退回数量、原因、暂缓额不可改。
                CREATE TRIGGER IF NOT EXISTS trg_quality_cases_guard
                BEFORE UPDATE ON quality_cases
                WHEN NEW.production_record_id != OLD.production_record_id
                  OR NEW.worker_id != OLD.worker_id OR NEW.quantity != OLD.quantity
                  OR NEW.reason != OLD.reason OR NEW.evidence IS NOT OLD.evidence
                  OR NEW.withheld_cents != OLD.withheld_cents OR NEW.opened_at != OLD.opened_at
                BEGIN
                  SELECT RAISE(ABORT, '质量案件的事实字段不可修改，仅允许复核决定');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_quality_cases_no_delete
                BEFORE DELETE ON quality_cases
                BEGIN
                  SELECT RAISE(ABORT, '质量案件不允许删除');
                END;
                """
            )
            # 封账后的批次/支付行不允许改状态或金额（INSERT 新批次/行是允许的）。
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_payment_batches_sealed_guard
                BEFORE UPDATE ON payment_batches
                WHEN OLD.status = 'sealed'
                 AND (NEW.status != 'sealed' OR NEW.total_cents != OLD.total_cents
                      OR NEW.period_start != OLD.period_start OR NEW.period_end != OLD.period_end)
                BEGIN
                  SELECT RAISE(ABORT, '批次已封账，不允许修改');
                END;
                """
            )

    # --- 基础工具 ---
    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur

    def query(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def get(self, table, row_id):
        return self.query_one(f"SELECT * FROM {table} WHERE id=?", (row_id,))

    def insert(self, table, **fields):
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        return cur.lastrowid

    def audit(self, actor, action, entity, entity_id=None, payload=None):
        self.insert(
            "audit_log",
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            created_at=now_ts(),
        )

    @staticmethod
    def row_hash(*parts):
        h = hashlib.sha256()
        h.update("|".join("" if p is None else str(p) for p in parts).encode("utf-8"))
        return h.hexdigest()

    def close(self):
        with self._lock:
            self._conn.close()
