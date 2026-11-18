"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

records 表保存流水线记录；diffs 表保存 compare 产出的差异集合报告，
两者均以稳定键幂等写入，重跑不产生重复行。
"""
import json, sqlite3
from .config import DB_PATH


def connect(db_path=None):
    con = sqlite3.connect(str(db_path or DB_PATH))
    con.execute("CREATE TABLE IF NOT EXISTS records("
                "key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS diffs("
                "pair_key TEXT PRIMARY KEY, report TEXT NOT NULL)")
    return con


def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))


def commit(con): con.commit()


def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


def save_diff_report(con, pair_key, report):
    """幂等保存一份差异报告；调用方负责 commit（支持逐对失败恢复）。"""
    con.execute("INSERT INTO diffs(pair_key,report) VALUES(?,?) "
                "ON CONFLICT(pair_key) DO UPDATE SET report=excluded.report",
                (pair_key, json.dumps(report, ensure_ascii=False,
                                       sort_keys=True)))


def load_diff_report(con, pair_key):
    row = con.execute("SELECT report FROM diffs WHERE pair_key=?",
                      (pair_key,)).fetchone()
    return json.loads(row[0]) if row else None


def diff_count(con):
    return con.execute("SELECT COUNT(*) FROM diffs").fetchone()[0]
