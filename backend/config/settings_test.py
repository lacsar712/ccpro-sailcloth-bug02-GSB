"""本地/CI 无 Postgres 时的测试配置：SQLite 文件库（支持跨线程并发用例）。"""

from django.db.backends.signals import connection_created

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "/tmp/sailcloth_test.sqlite3",
        "TEST": {"NAME": "/tmp/sailcloth_test.sqlite3"},
        "OPTIONS": {
            # 并发用例中多个线程争抢写锁：
            # IMMEDIATE 让事务开始即排队取写锁，busy_timeout 负责等待，
            # 避免 DEFERRED 事务内读转写时 SQLITE_BUSY 立即失败。
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
        },
    }
}


def _sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000;")


connection_created.connect(_sqlite_pragmas)
