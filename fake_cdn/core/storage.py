"""
SQLite 存储模块
用于高效存储和查询 CDN 日志数据
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager


class CDNLogStorage:
    """CDN 日志 SQLite 存储"""

    def __init__(self, db_path: str = "./output/cdn_logs.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with self._get_conn() as conn:
            self._create_tables(conn)

    @contextmanager
    def _get_conn(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _create_tables(self, conn):
        """创建表和索引"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cdn_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time INTEGER NOT NULL,
                tenant_id TEXT,
                domain TEXT NOT NULL,
                country TEXT,
                region TEXT,
                interval INTEGER,
                bw INTEGER,
                flux INTEGER,
                bs_bw INTEGER,
                bs_flux INTEGER,
                req_num INTEGER,
                hit_num INTEGER,
                bs_num INTEGER,
                bs_fail_num INTEGER,
                hit_flux INTEGER,
                http_code_2xx INTEGER,
                http_code_3xx INTEGER,
                http_code_4xx INTEGER,
                http_code_5xx INTEGER,
                bs_http_code_2xx INTEGER,
                bs_http_code_3xx INTEGER,
                bs_http_code_4xx INTEGER,
                bs_http_code_5xx INTEGER
            )
        """)

        # 创建索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_start_time ON cdn_logs(start_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_domain ON cdn_logs(domain)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_time_domain ON cdn_logs(start_time, domain)")

    def insert_logs(self, logs: List[Dict]):
        """批量插入日志"""
        if not logs:
            return

        with self._get_conn() as conn:
            conn.executemany("""
                INSERT INTO cdn_logs (
                    start_time, tenant_id, domain, country, region, interval,
                    bw, flux, bs_bw, bs_flux,
                    req_num, hit_num, bs_num, bs_fail_num, hit_flux,
                    http_code_2xx, http_code_3xx, http_code_4xx, http_code_5xx,
                    bs_http_code_2xx, bs_http_code_3xx, bs_http_code_4xx, bs_http_code_5xx
                ) VALUES (
                    :start_time, :tenantId, :domain, :country, :region, :interval,
                    :bw, :flux, :bs_bw, :bs_flux,
                    :req_num, :hit_num, :bs_num, :bs_fail_num, :hit_flux,
                    :http_code_2xx, :http_code_3xx, :http_code_4xx, :http_code_5xx,
                    :bs_http_code_2xx, :bs_http_code_3xx, :bs_http_code_4xx, :bs_http_code_5xx
                )
            """, logs)

        print(f"[存储] 已插入 {len(logs)} 条日志到 SQLite")

    def query_logs(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        domain: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """查询日志"""
        query = "SELECT * FROM cdn_logs WHERE 1=1"
        params = []

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        query += " ORDER BY start_time ASC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_time_range(self) -> Tuple[Optional[int], Optional[int]]:
        """获取数据时间范围"""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT MIN(start_time) as min_time, MAX(start_time) as max_time FROM cdn_logs"
            )
            row = cursor.fetchone()
            if row:
                return row["min_time"], row["max_time"]
            return None, None

    def get_domains(self) -> List[str]:
        """获取所有域名列表"""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT DISTINCT domain FROM cdn_logs ORDER BY domain")
            return [row["domain"] for row in cursor.fetchall()]

    def get_stats(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> Dict:
        """获取统计信息"""
        query = """
            SELECT
                COUNT(*) as total_records,
                COUNT(DISTINCT domain) as domain_count,
                SUM(bw) as total_bw,
                SUM(flux) as total_flux,
                SUM(req_num) as total_requests,
                SUM(hit_num) as total_hits,
                SUM(bs_num) as total_bs,
                SUM(bs_fail_num) as total_bs_fail
            FROM cdn_logs
            WHERE 1=1
        """
        params = []

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return dict(row) if row else {}

    def get_aggregated_by_time(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        domain: Optional[str] = None,
        interval_ms: int = 300000  # 默认5分钟
    ) -> List[Dict]:
        """按时间聚合数据（用于图表）"""
        query = """
            SELECT
                (start_time / ?) * ? as time_bucket,
                SUM(bw) as total_bw,
                SUM(flux) as total_flux,
                SUM(bs_bw) as total_bs_bw,
                SUM(bs_flux) as total_bs_flux,
                SUM(req_num) as total_requests,
                SUM(hit_num) as total_hits,
                SUM(bs_num) as total_bs,
                SUM(bs_fail_num) as total_bs_fail,
                SUM(http_code_2xx) as total_2xx,
                SUM(http_code_3xx) as total_3xx,
                SUM(http_code_4xx) as total_4xx,
                SUM(http_code_5xx) as total_5xx,
                SUM(bs_http_code_2xx) as total_bs_2xx,
                SUM(bs_http_code_3xx) as total_bs_3xx,
                SUM(bs_http_code_4xx) as total_bs_4xx,
                SUM(bs_http_code_5xx) as total_bs_5xx
            FROM cdn_logs
            WHERE 1=1
        """
        params = [interval_ms, interval_ms]

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        query += " GROUP BY time_bucket ORDER BY time_bucket"

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_aggregated_by_domain(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 10
    ) -> List[Dict]:
        """按域名聚合数据（用于排行榜）"""
        query = """
            SELECT
                domain,
                SUM(flux) as total_flux,
                SUM(req_num) as total_requests,
                AVG(CASE WHEN req_num > 0 THEN hit_num * 100.0 / req_num ELSE 0 END) as avg_hit_rate
            FROM cdn_logs
            WHERE 1=1
        """
        params = []

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        query += " GROUP BY domain ORDER BY total_flux DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def clear(self):
        """清空数据"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM cdn_logs")
        print("[存储] 已清空所有日志")

    def get_record_count(self) -> int:
        """获取记录总数"""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM cdn_logs")
            row = cursor.fetchone()
            return row["cnt"] if row else 0


def get_default_storage() -> CDNLogStorage:
    """获取默认存储实例"""
    from pathlib import Path
    project_root = Path(__file__).parent.parent.parent
    db_path = project_root / "output" / "cdn_logs.db"
    return CDNLogStorage(str(db_path))
