import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from src.logger import log

class OrderDatabase:
    """Управление базой данных заказов на SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Создаем директорию базы, если не существует
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
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
            # Очищаем временные сбои API 404, чтобы при обновлении модели заказы пошли в Gemini 3.6 Flash
            cursor.execute("DELETE FROM orders WHERE llm_reason LIKE '%API Error%' OR status = 'REJECTED_LOW_PRICE';")
            conn.commit()

    def order_exists(self, order_id: str) -> bool:
        """Проверить, обрабатывался ли заказ ранее."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,))
            return cursor.fetchone() is not None

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Получить информацию о заказе."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
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
        """Сохранить или обновить заказ в БД."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO orders (
                    id, title, description, price, status,
                    feasible, confidence, llm_reason, proposal_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    feasible = excluded.feasible,
                    confidence = excluded.confidence,
                    llm_reason = excluded.llm_reason,
                    proposal_message = excluded.proposal_message;
            """, (
                order_id, title, description, price, status,
                1 if feasible else 0, confidence, llm_reason, proposal_message
            ))
            conn.commit()

    def mark_applied(self, order_id: str, proposal_message: Optional[str] = None):
        """Отметить, что на заказ был отправлен отклик."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if proposal_message:
                cursor.execute("""
                    UPDATE orders
                    SET status = 'APPLIED', proposal_message = ?, applied_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (proposal_message, order_id))
            else:
                cursor.execute("""
                    UPDATE orders
                    SET status = 'APPLIED', applied_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (order_id,))
            conn.commit()

    def mark_rejected_manual(self, order_id: str):
        """Отметить, что заказ был отклонен пользователем в Telegram."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE orders
                SET status = 'REJECTED_MANUAL', llm_reason = 'Отклонен пользователем в Telegram'
                WHERE id = ?;
            """, (order_id,))
            conn.commit()

    def mark_expired(self, order_id: str):
        """Отметить, что истекло время ожидания решения (таймаут)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE orders
                SET status = 'EXPIRED_TIMEOUT', llm_reason = 'Истекло время ожидания решения в Telegram'
                WHERE id = ?;
            """, (order_id,))
            conn.commit()

    def mark_hidden(self, order_id: str):
        """Отметить, что заказ был скрыт на сайте."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE orders
                SET status = 'HIDDEN', hidden_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (order_id,))
            conn.commit()
