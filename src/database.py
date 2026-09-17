import sqlite3
import re
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import contextmanager
from src.logger import log

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


class OrderDatabase:
    """Универсальное хранилище заказов: поддерживает удаленный PostgreSQL и локальный SQLite."""

    def __init__(self, db_path: str = "data/workzilla.db", db_url: Optional[str] = None):
        self.db_path = db_path
        self.db_url = db_url
        self.is_postgres = bool(self.db_url and (
            self.db_url.startswith("postgresql://") or self.db_url.startswith("postgres://")
        ))

        self._pg_conn = None
        self.ph = "%s" if self.is_postgres else "?"

        if self.is_postgres:
            if not HAS_PSYCOPG2:
                raise ImportError("Для работы с PostgreSQL требуется библиотека psycopg2-binary: pip install psycopg2-binary")
            clean_url = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", self.db_url)
            log.info(f"[cyan]Подключение к удаленной базе данных PostgreSQL ({clean_url})...[/cyan]")
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            log.info(f"[cyan]Использование локальной базы данных SQLite ({self.db_path})...[/cyan]")

        self._init_db()

    @contextmanager
    def _get_cursor(self):
        """Контекстный менеджер курсора с автопереподключением к PostgreSQL."""
        if self.is_postgres:
            if self._pg_conn is None or self._pg_conn.closed:
                self._pg_conn = psycopg2.connect(self.db_url, cursor_factory=RealDictCursor)
                self._pg_conn.autocommit = True
            try:
                with self._pg_conn.cursor() as cur:
                    yield cur
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                log.warning(f"[yellow]Переподключение к PostgreSQL: {e}[/yellow]")
                self._pg_conn = psycopg2.connect(self.db_url, cursor_factory=RealDictCursor)
                self._pg_conn.autocommit = True
                with self._pg_conn.cursor() as cur:
                    yield cur
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                yield conn.cursor()

    def _init_db(self):
        """Создание таблиц и индексов при старте."""
        with self._get_cursor() as cursor:
            if self.is_postgres:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id VARCHAR(64) PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        price DOUBLE PRECISION DEFAULT 0,
                        status VARCHAR(64) NOT NULL,
                        feasible INTEGER DEFAULT 0,
                        confidence REAL DEFAULT 0.0,
                        llm_reason TEXT,
                        proposal_message TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        applied_at TIMESTAMP WITH TIME ZONE,
                        hidden_at TIMESTAMP WITH TIME ZONE
                    );
                    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                    DELETE FROM orders WHERE status NOT IN ('APPLIED');
                """)
            else:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        price REAL DEFAULT 0,
                        status TEXT NOT NULL,
                        feasible INTEGER DEFAULT 0,
                        confidence REAL DEFAULT 0.0,
                        llm_reason TEXT,
                        proposal_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        applied_at TIMESTAMP,
                        hidden_at TIMESTAMP
                    );
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
                cursor.execute("DELETE FROM orders WHERE status NOT IN ('APPLIED');")

    def order_exists(self, order_id: str) -> bool:
        """Проверить, обрабатывался ли заказ ранее."""
        with self._get_cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM orders WHERE id = {self.ph}", (order_id,))
            return cursor.fetchone() is not None

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Получить информацию о заказе."""
        with self._get_cursor() as cursor:
            cursor.execute(f"SELECT * FROM orders WHERE id = {self.ph}", (order_id,))
            row = cursor.fetchone()
            if row:
                res = dict(row)
                if "price" in res and res["price"] is not None:
                    res["price"] = float(res["price"])
                return res
            return None

    def save_order(
        self,
        order_id: str,
        title: str,
        description: str,
        price: float,
        status: str,
        feasible: bool = False,
        confidence: float = 0.0,
        llm_reason: str = "",
        proposal_message: str = ""
    ):
        """Сохранить или обновить заказ в БД (upsert)."""
        p = self.ph
        with self._get_cursor() as cursor:
            query = f"""
                INSERT INTO orders (
                    id, title, description, price, status,
                    feasible, confidence, llm_reason, proposal_message
                ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(id) DO UPDATE SET
                    status = EXCLUDED.status,
                    feasible = EXCLUDED.feasible,
                    confidence = EXCLUDED.confidence,
                    llm_reason = EXCLUDED.llm_reason,
                    proposal_message = EXCLUDED.proposal_message;
            """
            cursor.execute(query, (
                order_id, title, description, price, status,
                1 if feasible else 0, confidence, llm_reason, proposal_message
            ))

    def mark_applied(self, order_id: str, proposal_message: Optional[str] = None):
        """Отметить, что на заказ был отправлен отклик."""
        p = self.ph
        with self._get_cursor() as cursor:
            if proposal_message:
                cursor.execute(f"""
                    UPDATE orders
                    SET status = 'APPLIED', proposal_message = {p}, applied_at = CURRENT_TIMESTAMP
                    WHERE id = {p};
                """, (proposal_message, order_id))
            else:
                cursor.execute(f"""
                    UPDATE orders
                    SET status = 'APPLIED', applied_at = CURRENT_TIMESTAMP
                    WHERE id = {p};
                """, (order_id,))

    def mark_rejected_manual(self, order_id: str):
        """Отметить, что заказ был отклонен пользователем в Telegram."""
        p = self.ph
        with self._get_cursor() as cursor:
            cursor.execute(f"""
                UPDATE orders
                SET status = 'REJECTED_MANUAL', llm_reason = 'Отклонен пользователем в Telegram'
                WHERE id = {p};
            """, (order_id,))

    def mark_expired(self, order_id: str):
        """Отметить, что истекло время ожидания решения (таймаут)."""
        p = self.ph
        with self._get_cursor() as cursor:
            cursor.execute(f"""
                UPDATE orders
                SET status = 'EXPIRED_TIMEOUT', llm_reason = 'Истекло время ожидания решения в Telegram'
                WHERE id = {p};
            """, (order_id,))

    def mark_hidden(self, order_id: str):
        """Отметить, что заказ был скрыт на сайте."""
        p = self.ph
        with self._get_cursor() as cursor:
            cursor.execute(f"""
                UPDATE orders
                SET status = 'HIDDEN', hidden_at = CURRENT_TIMESTAMP
                WHERE id = {p};
            """, (order_id,))

