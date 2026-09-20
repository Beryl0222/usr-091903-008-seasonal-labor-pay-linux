"""应用门面：组装数据库与各领域服务。"""

from . import SERVICE_ID, SERVICE_NAME
from .appeals import Appeals
from .attendance import Attendance
from .db import Database
from .payroll import Payroll
from .production import Production
from .registry import Registry
from .scheduling import Scheduling


class Service:
    def __init__(self, db_path=":memory:"):
        self.db = Database(db_path)
        self.registry = Registry(self.db)
        self.scheduling = Scheduling(self.db)
        self.attendance = Attendance(self.db)
        self.production = Production(self.db)
        self.payroll = Payroll(self.db)
        self.appeals = Appeals(self.db)

    def health(self):
        return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}

    def close(self):
        self.db.close()
