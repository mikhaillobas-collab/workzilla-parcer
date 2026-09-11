import asyncio
import random
from typing import List
from src.config import AppConfig
from src.database import OrderDatabase
from src.llm_evaluator import GeminiEvaluator, EvaluationResult
from src.browser_client import WorkzillaBrowserClient, WorkzillaOrder
from src.logger import log

class OrderManager:
    """Оркестратор обработки заказов: фильтрация -> оценка LLM -> отклик или скрытие."""

    def __init__(
        self,
        config: AppConfig,
        db: OrderDatabase,
        evaluator: GeminiEvaluator,
        browser: WorkzillaBrowserClient
    ):
        self.config = config
        self.db = db
        self.evaluator = evaluator
        self.browser = browser

    def check_stop_words(self, title: str, description: str) -> str:
        """Проверка наличия стоп-слов в заказе. Возвращает найденное стоп-слово или пустую строку."""
        full_text = f"{title} {description}".lower()
        for word in self.config.stop_words:
            if word in full_text:
                return word
        return ""

    async def process_order(self, order: WorkzillaOrder):
        """Полный цикл обработки отдельного заказа."""
        # 1. Проверка дедупликации
        if self.db.order_exists(order.order_id):
            return

        log.info(f"\n[bold cyan]------------------ Новый заказ #{order.order_id} ------------------[/bold cyan]")
        log.info(f"[bold]Заголовок:[/bold] {order.title}")
        log.info(f"[bold]Бюджет:[/bold] {order.price} руб.")

        # 2. Проверка по минимальной цене
        if 0 < order.price < self.config.filters.min_price:
            log.info(f"[dim yellow]Отсеян по цене ({order.price} < {self.config.filters.min_price} руб.)[/dim yellow]")
            self.db.save_order(
                order_id=order.order_id,
                title=order.title,
                description=order.description,
                price=order.price,
                status="REJECTED_LOW_PRICE",
                feasible=False,
                llm_reason="Бюджет ниже порога фильтра"
            )
            if self.config.filters.hide_rejected_on_site:
                await self.browser.hide_order(order)
            return

        # 3. Проверка по стоп-словам
        matched_stopword = self.check_stop_words(order.title, order.description)
        if matched_stopword:
            log.info(f"[yellow]Отсеян по стоп-слову: [bold]'{matched_stopword}'[/bold][/yellow]")
            self.db.save_order(
                order_id=order.order_id,
                title=order.title,
                description=order.description,
                price=order.price,
                status="REJECTED_STOPWORD",
                feasible=False,
                llm_reason=f"Обнаружено стоп-слово: {matched_stopword}"
            )
            if self.config.filters.hide_rejected_on_site:
                await self.browser.hide_order(order)
            return

        # 4. Оценка через Gemini LLM (через Proxy)
        log.info("[magenta]Отправка на анализ в Gemini LLM...[/magenta]")
        eval_result: EvaluationResult = await self.evaluator.evaluate_order(
            title=order.title,
            description=order.description,
            price=order.price
        )

        log.info(f"[bold]Вердикт LLM:[/bold] Feasible={eval_result.feasible} (Confidence: {eval_result.confidence})")
        log.info(f"[bold]Причина:[/bold] {eval_result.reason}")

        # 5. Принятие решения
        if eval_result.feasible and eval_result.confidence >= self.config.filters.min_confidence:
            log.info(f"[bold green]>>> ЗАКАЗ ПОДХОДИТ ДЛЯ 100% LLM ВЫПОЛНЕНИЯ! <<<[/bold green]")
            log.info(f"[green]Сгенерированный отклик ({len(eval_result.proposal_message)} симв.):[/green]\n{eval_result.proposal_message}")

            # Сохраняем в БД со статусом PENDING_APPLY
            self.db.save_order(
                order_id=order.order_id,
                title=order.title,
                description=order.description,
                price=order.price,
                status="PENDING_APPLY",
                feasible=True,
                confidence=eval_result.confidence,
                llm_reason=eval_result.reason,
                proposal_message=eval_result.proposal_message
            )

            # Рандомизированная задержка перед откликом для имитации человека
            delay = random.randint(
                self.config.delays.min_apply_delay_sec,
                self.config.delays.max_apply_delay_sec
            )
            log.info(f"[cyan]Имитация действий человека: пауза перед отправкой {delay} сек...[/cyan]")
            await asyncio.sleep(delay)

            # Отправка отклика через браузер
            success = await self.browser.apply_order(
                order=order,
                proposal_text=eval_result.proposal_message,
                dry_run=self.config.dry_run
            )

            if success:
                self.db.mark_applied(order.order_id)
        else:
            log.info("[dim red]Заказ не подходит для автономного выполнения LLM. Скрываем...[/dim red]")
            self.db.save_order(
                order_id=order.order_id,
                title=order.title,
                description=order.description,
                price=order.price,
                status="REJECTED_LLM",
                feasible=False,
                confidence=eval_result.confidence,
                llm_reason=eval_result.reason
            )
            if self.config.filters.hide_rejected_on_site:
                await self.browser.hide_order(order)

    async def run_cycle(self):
        """Один цикл сканирования и обработки заказов."""
        orders = await self.browser.fetch_orders()
        if orders:
            log.info(f"[blue]Обнаружено заказов в ленте: {len(orders)}[/blue]")
            for order in orders:
                await self.process_order(order)
        else:
            log.debug("Новых заказов в ленте не обнаружено.")
