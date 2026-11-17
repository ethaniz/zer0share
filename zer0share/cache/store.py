from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import duckdb
import pandas as pd


_locks = {}
_registry_lock = threading.Lock()


class CacheStore:
    def __init__(self, path: Path, timeout: float = 120):
        self.path = Path(path).resolve()
        self.timeout = timeout
        with _registry_lock:
            self._thread_lock = _locks.setdefault(self.path, threading.Lock())

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise TimeoutError("timed out waiting for cache lock")
        try:
            with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("timed out waiting for cache lock") from None
                        time.sleep(0.05)
                try:
                    with duckdb.connect(str(self.path)) as connection:
                        self._initialize(connection)
                        yield connection
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            self._thread_lock.release()

    @staticmethod
    def _initialize(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS cache_schema (version INTEGER)")
        version = connection.execute("SELECT version FROM cache_schema").fetchall()
        if version not in ([], [(1,)], [(2,)]):
            raise ValueError("unsupported cache schema version")
        if version == [(1,)]:
            connection.execute("BEGIN")
            try:
                connection.execute("""
                    CREATE TABLE cache_rows_v2 (
                        api VARCHAR, scope VARCHAR, record_date VARCHAR,
                        payload VARCHAR, record_key VARCHAR,
                        PRIMARY KEY (api, scope, record_date, record_key)
                    )
                """)
                connection.execute("INSERT INTO cache_rows_v2 SELECT *, '' FROM cache_rows")
                connection.execute("DROP TABLE cache_rows")
                connection.execute("ALTER TABLE cache_rows_v2 RENAME TO cache_rows")
                connection.execute("UPDATE cache_schema SET version = 2")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        else:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS cache_rows (
                    api VARCHAR, scope VARCHAR, record_date VARCHAR, payload VARCHAR,
                    record_key VARCHAR,
                    PRIMARY KEY (api, scope, record_date, record_key)
                )
            """)
            if not version:
                connection.execute("INSERT INTO cache_schema VALUES (2)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cache_coverage (
                api VARCHAR, scope VARCHAR, start_date VARCHAR, end_date VARCHAR,
                expires_at DOUBLE,
                PRIMARY KEY (api, scope, start_date, end_date)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cache_state (
                name VARCHAR PRIMARY KEY, value DOUBLE
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cache_metrics (
                api VARCHAR PRIMARY KEY,
                lookups BIGINT DEFAULT 0, hits BIGINT DEFAULT 0,
                partial_hits BIGINT DEFAULT 0, misses BIGINT DEFAULT 0,
                remote_calls BIGINT DEFAULT 0, remote_errors BIGINT DEFAULT 0,
                written_rows BIGINT DEFAULT 0, empty_responses BIGINT DEFAULT 0
            )
        """)

    @staticmethod
    def count(connection, api, **increments):
        allowed = {
            "lookups", "hits", "partial_hits", "misses", "remote_calls",
            "remote_errors", "written_rows", "empty_responses",
        }
        if not increments or set(increments) - allowed:
            raise ValueError("invalid cache metric")
        connection.execute("INSERT INTO cache_metrics (api) VALUES (?) ON CONFLICT DO NOTHING", [api])
        assignments = ", ".join(f"{name} = {name} + ?" for name in increments)
        connection.execute(
            f"UPDATE cache_metrics SET {assignments} WHERE api = ?",
            [*increments.values(), api],
        )

    @staticmethod
    def intervals(connection, api, scope, now):
        return connection.execute("""
            SELECT start_date, end_date FROM cache_coverage
            WHERE api = ? AND scope = ? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY start_date, end_date
        """, [api, scope, now]).fetchall()

    @staticmethod
    def remove_coverage(connection, api, scope, start, end):
        covered = connection.execute("""
            SELECT start_date, end_date, expires_at FROM cache_coverage
            WHERE api = ? AND scope = ? AND start_date <= ? AND end_date >= ?
        """, [api, scope, end, start]).fetchall()
        connection.execute("""
            DELETE FROM cache_coverage
            WHERE api = ? AND scope = ? AND start_date <= ? AND end_date >= ?
        """, [api, scope, end, start])
        for covered_start, covered_end, expires in covered:
            fragments = []
            if covered_start < start:
                before = dt.datetime.strptime(start, "%Y%m%d").date() - dt.timedelta(days=1)
                fragments.append((covered_start, before.strftime("%Y%m%d")))
            if covered_end > end:
                after = dt.datetime.strptime(end, "%Y%m%d").date() + dt.timedelta(days=1)
                fragments.append((after.strftime("%Y%m%d"), covered_end))
            for fragment_start, fragment_end in fragments:
                connection.execute("""
                    INSERT INTO cache_coverage VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT DO UPDATE SET expires_at = excluded.expires_at
                """, [api, scope, fragment_start, fragment_end, expires])

    @staticmethod
    def write(connection, api, scope, start, end, frame, date_column, expires, key_columns=()):
        payloads = json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))
        rows = [
            (
                api, scope, str(record[date_column]) if date_column else "",
                json.dumps(record),
                json.dumps([record[column] for column in key_columns]) if key_columns else "",
            )
            for record in payloads
        ]
        connection.execute("BEGIN")
        try:
            connection.execute("""
                DELETE FROM cache_rows
                WHERE api = ? AND scope = ? AND record_date BETWEEN ? AND ?
            """, [api, scope, start, end])
            if rows:
                connection.executemany("INSERT INTO cache_rows VALUES (?, ?, ?, ?, ?)", rows)
            CacheStore.remove_coverage(connection, api, scope, start, end)
            connection.execute("""
                INSERT INTO cache_coverage VALUES (?, ?, ?, ?, ?)
            """, [api, scope, start, end, expires])
            CacheStore.count(
                connection, api, written_rows=len(rows), empty_responses=int(not rows),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def read(connection, api, scope, start, end):
        records = connection.execute("""
            SELECT payload FROM cache_rows
            WHERE api = ? AND scope = ? AND record_date BETWEEN ? AND ?
            ORDER BY record_date DESC, record_key
        """, [api, scope, start, end]).fetchall()
        return [json.loads(record[0]) for record in records]

    def status(self):
        with self.connect() as connection:
            return connection.execute("""
                SELECT coverage.api,
                    (SELECT count(*) FROM cache_rows AS records
                     WHERE records.api = coverage.api) AS rows,
                    count(DISTINCT coverage.scope) AS scopes,
                    count(*) AS coverage_ranges
                FROM cache_coverage AS coverage GROUP BY coverage.api ORDER BY coverage.api
            """).fetchdf()

    def metrics(self):
        with self.connect() as connection:
            return connection.execute("""
                SELECT *, CASE WHEN lookups = 0 THEN 0.0
                    ELSE hits * 1.0 / lookups END AS hit_rate
                FROM cache_metrics ORDER BY api
            """).fetchdf()

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination == self.path:
            raise ValueError("backup must use a different path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("CHECKPOINT")
            temporary_path = None
            try:
                with self.path.open("rb") as source, tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".cache-backup-", delete=False,
                ) as target:
                    temporary_path = Path(target.name)
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.link(temporary_path, destination)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        return destination

    def clear(self, api=None, *, ts_code=None, start_date=None, end_date=None):
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                scopes = connection.execute(
                    "SELECT DISTINCT api, scope FROM cache_coverage"
                    + (" WHERE api = ?" if api else ""),
                    [api] if api else [],
                ).fetchall()
                for scope_api, scope in scopes:
                    symbol = json.loads(scope).get("ts_code") if scope.startswith("{") else scope
                    if ts_code is not None and symbol != ts_code:
                        continue
                    if start_date is None and end_date is None:
                        for table in ("cache_rows", "cache_coverage"):
                            connection.execute(
                                f"DELETE FROM {table} WHERE api = ? AND scope = ?", [scope_api, scope],
                            )
                        continue
                    if scope_api == "stock_basic":
                        continue
                    start = start_date or "00010101"
                    end = end_date or "99991231"
                    connection.execute("""
                        DELETE FROM cache_rows
                        WHERE api = ? AND scope = ? AND record_date BETWEEN ? AND ?
                    """, [scope_api, scope, start, end])
                    self.remove_coverage(connection, scope_api, scope, start, end)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
