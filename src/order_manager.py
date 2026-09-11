import asyncio
import random
from typing import List, Optional
from src.config import AppConfig
from src.database import OrderDatabase
from src.llm_evaluator import GeminiEvaluator, EvaluationResult
from src.browser_client import WorkzillaBrowserClient, WorkzillaOrder
from src.telegram_bot import TelegramBotHandler
from src.logger import log

class OrderManager:
    """Оркестратор обработки заказов: фильтрация -> оценка LLM -> согласование в TG / отклик."""

    def __init__(
        self,
        config: AppConfig,
        db: OrderDatabase,
        evaluator: GeminiEvaluator,
        browser: WorkzillaBrowserClient,
        telegram_bot: Optional[TelegramBotHandler] = None,
    ):
        self.config = config
        self.db = db
        self.evaluator = evaluator
        self.browser = browser
        self.telegram_bot = telegram_bot

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
        existing = self.db.get_order(order.order_id)
        if existing:
            # Если отклик отправлен, ожидает решения в TG или отсеян — пропускаем
            if existing["status"] in (
                "APPLIED",
                "PENDING_APPLY",
                "WAITING_APPROVAL",
                "REJECTED_STOPWORD",
                "REJECTED_MANUAL",
                "EXPIRED_TIMEOUT",
            ):
                return
            # Если отсеян LLM, но отсеян НЕ из-за сбоя API — пропускаем
            if existing["status"] == "REJECTED_LLM":
                reason = existing.get("llm_reason") or ""
                if not reason.startswith("API Error"):
                    return
            # Если ранее был отсеян по цене, и текущая цена все еще ниже порога — пропускаем
            if existing["status"] == "REJECTED_LOW_PRICE" and (order.price <= 0 or order.price < self.config.filters.min_price):
                return

        log.info(f"\n[bold cyan]------------------ Новый заказ #{order.order_id} ------------------[/bold cyan]")
        log.info(f"[bold]Заголовок:[/bold] {order.title}")
        log.info(f"[bold]Бюджет:[/bold] {order.price} руб.")

        # 2. Проверка по минимальной цене
        if 0 < order.price < self.config.filters.min_price:
            log.info(f"[dim yellow]Отсеян по цене ({order.price:.0f} < {self.config.filters.min_price} руб.)[/dim yellow]")
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

            if self.telegram_bot and self.telegram_bot.is_ready():
                # Сохраняем в БД со статусом WAITING_APPROVAL
                self.db.save_order(
                    order_id=order.order_id,
                    title=order.title,
                    description=order.description,
                    price=order.price,
                    status="WAITING_APPROVAL",
                    feasible=True,
                    confidence=eval_result.confidence,
                    llm_reason=eval_result.reason,
                    proposal_message=eval_result.proposal_message
                )

                # Отправляем карточку в Telegram с инлайн-кнопками и таймаутом 15 мин
                await self.telegram_bot.send_order_approval(order, eval_result)
            else:
                # Резервный автономный режим (если Telegram бот не настроен)
                log.warning("[yellow]Telegram-бот не настроен. Отправка отклика в автономном режиме...[/yellow]")
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

                delay = random.randint(
                    self.config.delays.min_apply_delay_sec,
                    self.config.delays.max_apply_delay_sec
                )
                log.info(f"[cyan]Имитация действий человека: пауза перед отправкой {delay} сек...[/cyan]")
                await asyncio.sleep(delay)

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
            # Считаем, сколько из них действительно новые (не в БД)
            new_count = 0
            for o in orders:
                ext = self.db.get_order(o.order_id)
                is_new = (
                    not ext 
                    or (ext["status"] == "REJECTED_LOW_PRICE" and o.price >= self.config.filters.min_price)
                    or (ext["status"] == "REJECTED_LLM" and (ext.get("llm_reason") or "").startswith("API Error"))
                )
                if is_new:
                    new_count += 1

            if new_count > 0:
                log.info(f"[blue]В ленте 'Новые': {len(orders)} заказов (из них новых на анализ: {new_count})[/blue]")
            else:
                log.info(f"[dim]В ленте 'Новые': {len(orders)} заказов (все уже проверены, ждем свежих пушей...)[/dim]")

            for order in orders:
                await self.process_order(order)
        else:
            log.debug("Новых заказов в ленте не обнаружено.")
