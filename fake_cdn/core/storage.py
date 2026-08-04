"""
SQLite 存储模块
用于高效存储和查询 CDN 日志数据
"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple


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
            self._migrate_project_column(conn)

    def _migrate_project_column(self, conn):
        """补齐租户、任务与配置版本字段（幂等迁移）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(cdn_logs)")}
        if "project" not in cols:
            conn.execute("ALTER TABLE cdn_logs ADD COLUMN project TEXT")
        if "config_version_id" not in cols:
            conn.execute("ALTER TABLE cdn_logs ADD COLUMN config_version_id INTEGER")
        if "generation_job_id" not in cols:
            conn.execute("ALTER TABLE cdn_logs ADD COLUMN generation_job_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project ON cdn_logs(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_time ON cdn_logs(project, start_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant_time ON cdn_logs(tenant_id, start_time)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenant_domain_time "
            "ON cdn_logs(tenant_id, domain, start_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenant_project_time "
            "ON cdn_logs(tenant_id, project, start_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenant_job "
            "ON cdn_logs(tenant_id, generation_job_id)"
        )
        # 老数据回填：project 保留原语义；tenant_id 不从项目反推，避免错误合并租户。
        conn.execute("""
            UPDATE cdn_logs
            SET project = COALESCE(NULLIF(tenant_id, ''), '默认')
            WHERE project IS NULL OR project = ''
        """)

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
                project TEXT,
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
                bs_http_code_5xx INTEGER,
                config_version_id INTEGER,
                generation_job_id TEXT
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

        # 兜底：每条记录确保有 project 字段；若无则回退到 tenantId 或 "默认"
        for log in logs:
            if not log.get("project"):
                log["project"] = log.get("tenantId") or "默认"
            log.setdefault("configVersionId", None)
            log.setdefault("generationJobId", None)

        with self._get_conn() as conn:
            conn.executemany("""
                INSERT INTO cdn_logs (
                    start_time, tenant_id, project, domain, country, region, interval,
                    bw, flux, bs_bw, bs_flux,
                    req_num, hit_num, bs_num, bs_fail_num, hit_flux,
                    http_code_2xx, http_code_3xx, http_code_4xx, http_code_5xx,
                    bs_http_code_2xx, bs_http_code_3xx, bs_http_code_4xx, bs_http_code_5xx,
                    config_version_id, generation_job_id
                ) VALUES (
                    :start_time, :tenantId, :project, :domain, :country, :region, :interval,
                    :bw, :flux, :bs_bw, :bs_flux,
                    :req_num, :hit_num, :bs_num, :bs_fail_num, :hit_flux,
                    :http_code_2xx, :http_code_3xx, :http_code_4xx, :http_code_5xx,
                    :bs_http_code_2xx, :bs_http_code_3xx, :bs_http_code_4xx, :bs_http_code_5xx,
                    :configVersionId, :generationJobId
                )
            """, logs)

        print(f"[存储] 已插入 {len(logs)} 条日志到 SQLite")

    def query_logs(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        domain: Optional[str] = None,
        project: Optional[str] = None,
        limit: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict]:
        """查询日志"""
        query = "SELECT * FROM cdn_logs WHERE 1=1"
        params = []

        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if project:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY start_time ASC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_time_range(
        self, project: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> Tuple[Optional[int], Optional[int]]:
        """获取数据时间范围"""
        query = "SELECT MIN(start_time) as min_time, MAX(start_time) as max_time FROM cdn_logs"
        params = []
        where = []
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        if project:
            where.append("project = ?")
            params.append(project)
        if where:
            query += " WHERE " + " AND ".join(where)
        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            if row:
                return row["min_time"], row["max_time"]
            return None, None

    def get_domains(
        self, project: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> List[str]:
        """获取所有域名列表（可按项目过滤）"""
        query = "SELECT DISTINCT domain FROM cdn_logs"
        params = []
        where = []
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        if project:
            where.append("project = ?")
            params.append(project)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY domain"
        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [row["domain"] for row in cursor.fetchall()]

    def get_projects(self, tenant_id: Optional[str] = None) -> List[str]:
        """获取租户内的项目列表。"""
        query = (
            "SELECT DISTINCT project FROM cdn_logs "
            "WHERE project IS NOT NULL AND project != ''"
        )
        params = []
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY project"
        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [row["project"] for row in cursor.fetchall()]

    def get_tenants(self) -> List[str]:
        """返回日志数据库中真实存在的租户，不推断或改写标识。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM cdn_logs "
                "WHERE tenant_id IS NOT NULL AND tenant_id != '' ORDER BY tenant_id"
            ).fetchall()
        return [row["tenant_id"] for row in rows]

    def get_domain_project_pairs(
        self, project: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> List[Dict]:
        """获取 (domain, project) 组合，用于域名管理页"""
        query = (
            "SELECT DISTINCT domain, project FROM cdn_logs "
            "WHERE project IS NOT NULL AND project != ''"
        )
        params: list = []
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY project, domain"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def get_stats(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
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

        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if project:
            query += " AND project = ?"
            params.append(project)

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return dict(row) if row else {}

    def get_aggregated_by_time(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        domain: Optional[str] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
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

        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if domain:
            query += " AND domain = ?"
            params.append(domain)

        if project:
            query += " AND project = ?"
            params.append(project)

        query += " GROUP BY time_bucket ORDER BY time_bucket"

        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_aggregated_by_domain(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
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

        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)

        if start_time:
            query += " AND start_time >= ?"
            params.append(start_time)

        if end_time:
            query += " AND start_time <= ?"
            params.append(end_time)

        if project:
            query += " AND project = ?"
            params.append(project)

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

    def get_record_count(self, tenant_id: Optional[str] = None) -> int:
        """获取记录数；前端调用必须传入 tenant_id。"""
        query = "SELECT COUNT(*) as cnt FROM cdn_logs"
        params = []
        if tenant_id:
            query += " WHERE tenant_id = ?"
            params.append(tenant_id)
        with self._get_conn() as conn:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return row["cnt"] if row else 0


def get_default_storage() -> CDNLogStorage:
    """获取默认存储实例，支持通过环境变量覆盖数据库路径。"""
    custom_db_path = os.environ.get("FAKE_CDN_DB_PATH")
    if custom_db_path:
        return CDNLogStorage(custom_db_path)

    from pathlib import Path
    project_root = Path(__file__).parent.parent.parent
    db_path = project_root / "output" / "cdn_logs.db"
    return CDNLogStorage(str(db_path))
