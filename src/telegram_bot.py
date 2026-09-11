import asyncio
import html
import random
from typing import Dict, Any, Optional
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from src.config import AppConfig
from src.database import OrderDatabase
from src.browser_client import WorkzillaBrowserClient, WorkzillaOrder
from src.llm_evaluator import EvaluationResult
from src.logger import log


class EditProposalState(StatesGroup):
    waiting_for_text = State()


class TelegramBotHandler:
    """Обработчик Telegram-бота для интерактивного согласования откликов на Work-zilla."""

    def __init__(
        self,
        config: AppConfig,
        db: OrderDatabase,
        browser: WorkzillaBrowserClient,
    ):
        self.config = config
        self.db = db
        self.browser = browser

        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.router = Router()
        self._polling_task: Optional[asyncio.Task] = None

        # Активные заказы на согласовании: order_id -> dict
        self.pending_orders: Dict[str, Dict[str, Any]] = {}

        if self.config.telegram.bot_token:
            self.bot = Bot(
                token=self.config.telegram.bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
            self.dp = Dispatcher(storage=MemoryStorage())
            self._register_handlers()
            self.dp.include_router(self.router)

    def is_ready(self) -> bool:
        """Проверка готовности Telegram-бота к отправке уведомлений."""
        return bool(self.bot and self.config.telegram.chat_id != 0)

    def _is_authorized(self, user_id: int, chat_id: int) -> bool:
        """Проверка доступа: если TELEGRAM_CHAT_ID задан, разрешаем только его."""
        if self.config.telegram.chat_id == 0:
            return True
        return user_id == self.config.telegram.chat_id or chat_id == self.config.telegram.chat_id

    def _build_card_text(
        self,
        order: WorkzillaOrder,
        eval_result: EvaluationResult,
        proposal_text: str,
    ) -> str:
        """Формирование безопасного HTML-сообщения карточки заказа."""
        safe_title = html.escape(order.title)
        
        desc = (order.description or "").strip()
        if len(desc) > 800:
            desc = desc[:800] + "..."
        safe_desc = html.escape(desc)
        
        safe_reason = html.escape(eval_result.reason)
        safe_proposal = html.escape(proposal_text)
        confidence_pct = int(eval_result.confidence * 100)

        lines = [
            f"🎯 <b>Новый подходящий заказ #{order.order_id}</b>",
            "",
            f"💰 <b>Бюджет:</b> {order.price:.0f} руб.",
            f"📝 <b>Заголовок:</b> {safe_title}",
        ]

        if safe_desc:
            lines.extend([
                f"📄 <b>Описание:</b>",
                f"{safe_desc}",
            ])

        lines.extend([
            "",
            f"🧠 <b>Анализ AI ({confidence_pct}%):</b> {safe_reason}",
            "",
            f"💬 <b>Предложение отклика ({len(proposal_text)} симв.):</b>",
            f"<blockquote>{safe_proposal}</blockquote>",
            "",
            f"⏳ <i>Ожидание решения: {self.config.telegram.order_timeout_minutes} мин.</i>",
        ])

        return "\n".join(lines)

    def _build_card_keyboard(self, order_id: str) -> InlineKeyboardMarkup:
        """Формирование инлайн-кнопок для карточки заказа."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Откликнуться",
                        callback_data=f"apply:{order_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"reject:{order_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить текст",
                        callback_data=f"edit:{order_id}",
                    ),
                ],
            ]
        )

    def _register_handlers(self):
        """Регистрация команд и обработчиков обратного вызова в роутере."""

        @self.router.message(Command("start"))
        async def handle_start(message: Message):
            chat_id = message.chat.id
            user_id = message.from_user.id if message.from_user else chat_id
            auth = self._is_authorized(user_id, chat_id)
            icon = "🟢" if auth else "🟡"

            text = (
                f"🤖 <b>Work-zilla AI Agent Bot</b>\n\n"
                f"{icon} <b>Ваш Chat ID:</b> <code>{chat_id}</code>\n"
                f"<b>ID пользователя:</b> <code>{user_id}</code>\n\n"
            )

            if not auth:
                text += (
                    "⚠️ <i>Этот бот настроен на другой Chat ID.</i>\n"
                    f"Чтобы разрешить присылать заказы в этот чат, установите:\n"
                    f"<code>TELEGRAM_CHAT_ID={chat_id}</code> в настройках хостинга (BotHost)."
                )
            else:
                text += (
                    "✅ <b>Бот готов к работе!</b>\n"
                    "Когда парсер обнаружит подходящий заказ, сюда придет карточка с кнопками:\n"
                    "• <b>[ ✅ Откликнуться ]</b> — отправить ставку на биржу\n"
                    "• <b>[ ❌ Отклонить ]</b> — пропустить и скрыть заказ\n"
                    "• <b>[ ✏️ Изменить текст ]</b> — отредактировать отклик перед отправкой"
                )
            await message.answer(text)

        @self.router.callback_query(F.data.startswith("apply:"))
        async def handle_apply(callback: CallbackQuery):
            user_id = callback.from_user.id if callback.from_user else 0
            chat_id = callback.message.chat.id if callback.message else 0
            if not self._is_authorized(user_id, chat_id):
                await callback.answer("У вас нет прав для этого действия.", show_alert=True)
                return

            order_id = callback.data.split(":", 1)[1]
            info = self.pending_orders.get(order_id)
            if not info or info["status"] != "pending":
                await callback.answer("Заказ уже обработан или время ожидания истекло.", show_alert=True)
                return

            info["status"] = "applying"
            timer = info.get("timer_task")
            if timer and not timer.done():
                timer.cancel()

            await callback.answer("Отправка отклика...")

            try:
                base_text = info.get("card_text", "")
                await callback.message.edit_text(
                    f"{base_text}\n\n⏳ <b>Отправка отклика на Work-zilla...</b>",
                    reply_markup=None,
                )
            except Exception:
                pass

            # Рандомизированная задержка перед откликом для имитации человека
            delay = random.randint(
                self.config.delays.min_apply_delay_sec,
                self.config.delays.max_apply_delay_sec,
            )
            log.info(f"[cyan]Имитация действий человека: пауза перед отправкой {delay} сек...[/cyan]")
            await asyncio.sleep(delay)

            success = await self.browser.apply_order(
                order=info["order"],
                proposal_text=info["proposal_text"],
                dry_run=self.config.dry_run,
            )

            base_text = info.get("card_text", "")
            if success:
                self.db.mark_applied(order_id, proposal_message=info["proposal_text"])
                mode_note = " <i>(DRY-RUN режим: отклик записан, без реальной ставки)</i>" if self.config.dry_run else ""
                final_text = f"{base_text}\n\n✅ <b>Отклик успешно отправлен!</b>{mode_note}"
                log.info(f"[green]Заказ #{order_id}: отклик подтвержден пользователем и отправлен.[/green]")
            else:
                final_text = f"{base_text}\n\n⚠️ <b>Ошибка: не удалось отправить отклик на бирже (см. логи).</b>"
                log.error(f"[red]Заказ #{order_id}: ошибка при отправке отклика на Work-zilla.[/red]")

            try:
                await callback.message.edit_text(final_text, reply_markup=None)
            except Exception:
                pass

            self.pending_orders.pop(order_id, None)

        @self.router.callback_query(F.data.startswith("reject:"))
        async def handle_reject(callback: CallbackQuery):
            user_id = callback.from_user.id if callback.from_user else 0
            chat_id = callback.message.chat.id if callback.message else 0
            if not self._is_authorized(user_id, chat_id):
                await callback.answer("У вас нет прав для этого действия.", show_alert=True)
                return

            order_id = callback.data.split(":", 1)[1]
            info = self.pending_orders.get(order_id)
            if not info or info["status"] != "pending":
                await callback.answer("Заказ уже обработан или время ожидания истекло.", show_alert=True)
                return

            info["status"] = "rejected"
            timer = info.get("timer_task")
            if timer and not timer.done():
                timer.cancel()

            await callback.answer("Заказ отклонен.")

            self.db.mark_rejected_manual(order_id)
            log.info(f"[yellow]Заказ #{order_id} отклонен пользователем в Telegram.[/yellow]")

            base_text = info.get("card_text", "")
            final_text = f"{base_text}\n\n❌ <b>Заказ отклонен вами.</b>"

            try:
                await callback.message.edit_text(final_text, reply_markup=None)
            except Exception:
                pass

            if self.config.filters.hide_rejected_on_site:
                await self.browser.hide_order(info["order"])

            self.pending_orders.pop(order_id, None)

        @self.router.callback_query(F.data.startswith("edit:"))
        async def handle_edit_button(callback: CallbackQuery, state: FSMContext):
            user_id = callback.from_user.id if callback.from_user else 0
            chat_id = callback.message.chat.id if callback.message else 0
            if not self._is_authorized(user_id, chat_id):
                await callback.answer("У вас нет прав для этого действия.", show_alert=True)
                return

            order_id = callback.data.split(":", 1)[1]
            info = self.pending_orders.get(order_id)
            if not info or info["status"] != "pending":
                await callback.answer("Заказ уже обработан или время ожидания истекло.", show_alert=True)
                return

            await callback.answer()
            await state.set_state(EditProposalState.waiting_for_text)
            await state.update_data(editing_order_id=order_id)

            safe_cur = html.escape(info["proposal_text"])
            await callback.message.reply(
                f"✏️ <b>Редактирование отклика для заказа #{order_id}</b>\n\n"
                f"Текущий текст:\n<blockquote>{safe_cur}</blockquote>\n\n"
                f"<i>Пришлите новый текст в чат (или отправьте /cancel для отмены):</i>"
            )

        @self.router.message(EditProposalState.waiting_for_text)
        async def handle_new_proposal_text(message: Message, state: FSMContext):
            user_id = message.from_user.id if message.from_user else 0
            chat_id = message.chat.id
            if not self._is_authorized(user_id, chat_id):
                return

            data = await state.get_data()
            order_id = data.get("editing_order_id")
            await state.clear()

            if not message.text or message.text.strip().lower() in ("/cancel", "отмена"):
                await message.reply("Редактирование отклика отменено.")
                return

            info = self.pending_orders.get(order_id)
            if not info or info["status"] != "pending":
                await message.reply(f"Заказ #{order_id} уже обработан или время ожидания истекло.")
                return

            new_text = message.text.strip()
            max_chars = self.config.filters.max_proposal_chars
            warning_note = ""
            if len(new_text) > max_chars:
                warning_note = f"\n⚠️ <i>Предупреждение: длина {len(new_text)} симв. превышает лимит {max_chars}.</i>"

            info["proposal_text"] = new_text

            # Пересобираем карточку с новым текстом
            new_card_text = self._build_card_text(
                order=info["order"],
                eval_result=info["eval_result"],
                proposal_text=new_text,
            )
            info["card_text"] = new_card_text
            keyboard = self._build_card_keyboard(order_id)

            try:
                await self.bot.edit_message_text(
                    chat_id=info["chat_id"],
                    message_id=info["message_id"],
                    text=new_card_text,
                    reply_markup=keyboard,
                )
            except Exception as e:
                log.debug(f"Не удалось обновить карточку заказа #{order_id} в TG: {e}")

            await message.reply(
                f"✅ Текст отклика для заказа #{order_id} успешно обновлен!{warning_note}\n\n"
                f"Нажмите кнопку <b>[ ✅ Откликнуться ]</b> на карточке заказа выше для отправки."
            )

    async def _timeout_worker(self, order_id: str):
        """Фоновый таймер ожидания решения пользователя (15 минут)."""
        try:
            timeout_sec = max(1, self.config.telegram.order_timeout_minutes * 60)
            await asyncio.sleep(timeout_sec)

            info = self.pending_orders.get(order_id)
            if not info or info["status"] != "pending":
                return

            info["status"] = "expired"
            log.info(
                f"[yellow]Время ожидания решения по заказу #{order_id} истекло "
                f"({self.config.telegram.order_timeout_minutes} мин.). Заказ пропущен.[/yellow]"
            )

            base_text = info.get("card_text", "")
            expired_text = (
                f"{base_text}\n\n"
                f"⏰ <b>Время истекло ({self.config.telegram.order_timeout_minutes} мин.). Заказ пропущен.</b>"
            )

            try:
                await self.bot.edit_message_text(
                    chat_id=info["chat_id"],
                    message_id=info["message_id"],
                    text=expired_text,
                    reply_markup=None,
                )
            except Exception as e:
                log.debug(f"Не удалось обновить сообщение по таймауту #{order_id}: {e}")

            self.db.mark_expired(order_id)

            if self.config.filters.hide_rejected_on_site:
                await self.browser.hide_order(info["order"])

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Ошибка в таймауте для #{order_id}: {e}")
        finally:
            self.pending_orders.pop(order_id, None)

    async def send_order_approval(
        self,
        order: WorkzillaOrder,
        eval_result: EvaluationResult,
    ) -> bool:
        """Отправка карточки заказа в Telegram для подтверждения пользователем."""
        if not self.is_ready():
            log.warning("[yellow]Telegram Bot не сконфигурирован (нет токена или chat_id).[/yellow]")
            return False

        card_text = self._build_card_text(order, eval_result, eval_result.proposal_message)
        keyboard = self._build_card_keyboard(order.order_id)

        try:
            msg = await self.bot.send_message(
                chat_id=self.config.telegram.chat_id,
                text=card_text,
                reply_markup=keyboard,
            )

            timer_task = asyncio.create_task(self._timeout_worker(order.order_id))

            self.pending_orders[order.order_id] = {
                "order": order,
                "proposal_text": eval_result.proposal_message,
                "eval_result": eval_result,
                "card_text": card_text,
                "message_id": msg.message_id,
                "chat_id": self.config.telegram.chat_id,
                "timer_task": timer_task,
                "status": "pending",
            }

            log.info(
                f"[bold cyan]Заказ #{order.order_id} отправлен в Telegram! Ожидание решения {self.config.telegram.order_timeout_minutes} мин.[/bold cyan]"
            )
            return True

        except Exception as e:
            log.error(f"[red]Не удалось отправить карточку заказа #{order.order_id} в Telegram: {e}[/red]")
            return False

    async def start_polling(self):
        """Запуск цикла получения сообщений Telegram."""
        if not self.bot or not self.dp:
            return
        log.info("[cyan]Запуск Telegram-бота (polling)...[/cyan]")
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
            await self.dp.start_polling(self.bot, allowed_updates=["message", "callback_query"])
        except asyncio.CancelledError:
            log.info("[cyan]Остановка polling Telegram-бота...[/cyan]")
        except Exception as e:
            log.error(f"[red]Ошибка в цикле Telegram-бота: {e}[/red]")

    async def stop(self):
        """Корректная остановка бота и закрытие сессий."""
        # Отменяем все активные таймауты
        for info in list(self.pending_orders.values()):
            timer = info.get("timer_task")
            if timer and not timer.done():
                timer.cancel()
        self.pending_orders.clear()

        if self.dp:
            await self.dp.stop_polling()
        if self.bot:
            await self.bot.session.close()
        log.info("[green]Telegram-бот успешно остановлен.[/green]")
