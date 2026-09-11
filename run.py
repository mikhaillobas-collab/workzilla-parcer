import asyncio
import signal
import sys
from rich.panel import Panel
from rich.table import Table

from src.config import load_config
from src.database import OrderDatabase
from src.llm_evaluator import GeminiEvaluator
from src.browser_client import WorkzillaBrowserClient
from src.order_manager import OrderManager
from src.logger import log, console

async def main():
    config = load_config()

    # Красивая информационная панель при запуске
    table = Table(title="[bold green]Параметры запуска Work-zilla AI Agent[/bold green]", show_header=True)
    table.add_column("Параметр", style="cyan")
    table.add_column("Значение", style="magenta")

    table.add_row("Gemini Model", config.gemini.model)
    table.add_row("Proxy", config.proxy_url if config.proxy_url else "[red]Не задан (прямое подключение)[/red]")
    table.add_row("Browser Mode", f"{config.browser_mode} (CDP: {config.cdp_url if config.browser_mode == 'cdp' else 'N/A'})")
    table.add_row("Dry Run (Безопасный режим)", "[green]ВКЛЮЧЕН (без реальных ставок)[/green]" if config.dry_run else "[red]ВЫКЛЮЧЕН (БОЕВОЙ РЕЖИМ)[/red]")
    table.add_row("Минимальная цена заказа", f"{config.filters.min_price} руб.")
    table.add_row("Лимит символов отклика", str(config.filters.max_proposal_chars))
    table.add_row("Скрытие неподходящих", str(config.filters.hide_rejected_on_site))
    table.add_row("Загружено стоп-слов", str(len(config.stop_words)))
    table.add_row("База данных SQLite", config.database.path)

    console.print(table)

    if not config.gemini_api_key:
        log.warning("[bold yellow]Внимание: GEMINI_API_KEY не указан в .env файле![/bold yellow]")
        log.warning("[yellow]Запросы к Gemini будут отклоняться. Укажите ключ в .env перед боевым запуском.[/yellow]")

    db = OrderDatabase(config.database.path)
    evaluator = GeminiEvaluator(config)
    browser = WorkzillaBrowserClient(config)

    log.info("[cyan]Инициализация браузера...[/cyan]")
    connected = await browser.start()
    if not connected:
        log.error("[bold red]Не удалось запустить браузер. Завершение работы.[/bold red]")
        return

    manager = OrderManager(config=config, db=db, evaluator=evaluator, browser=browser)

    log.info("[bold green]Система мониторинга Work-zilla успешно запущена! Нажмите Ctrl+C для выхода.[/bold green]")

    stop_event = asyncio.Event()

    def handle_exit():
        log.info("[yellow]Получен сигнал завершения... Остановка...[/yellow]")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_exit)
        except NotImplementedError:
            # На Windows add_signal_handler может не поддерживаться для некоторых сигналов
            pass

    try:
        while not stop_event.is_set():
            await manager.run_cycle()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.delays.poll_interval_sec)
            except asyncio.TimeoutError:
                pass
    except (KeyboardInterrupt, SystemExit):
        log.info("[yellow]Прерывание с клавиатуры.[/yellow]")
    finally:
        log.info("[cyan]Закрытие браузера и сохранение данных...[/cyan]")
        await browser.close()
        log.info("[green]Работа завершена корректно.[/green]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
