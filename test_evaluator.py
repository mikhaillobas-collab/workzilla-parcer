import asyncio
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config import load_config
from src.database import OrderDatabase
from src.llm_evaluator import GeminiEvaluator
from src.logger import log, console
from rich.table import Table

async def run_tests():
    console.print("\n[bold cyan]=== Запуск тестов компонентов Work-zilla Bot ===[/bold cyan]\n")
    config = load_config()

    # 1. Тест загрузки конфигурации
    assert len(config.stop_words) > 0, "Список стоп-слов пуст!"
    log.info(f"[green][+] Конфигурация успешно загружена (стоп-слов: {len(config.stop_words)})[/green]")

    # 2. Тест фильтрации по стоп-словам
    test_cases_stopwords = [
        ("Оставить отзыв на Авито о покупке запчастей", True, "отзыв"),
        ("Обзвонить базу клиентов (50 номеров)", True, "обзвон"),
        ("Курьерская доставка документов", True, "доставка"),
        ("Написать статью про путешествия в Италию на 3000 знаков", False, ""),
        ("Спарсить каталог интернет-магазина на Python", False, ""),
    ]

    for text, should_match, expected_word in test_cases_stopwords:
        full_text = text.lower()
        matched = ""
        for w in config.stop_words:
            if w in full_text:
                matched = w
                break
        if should_match:
            assert bool(matched), f"Ожидалось срабатывание стоп-слова для: '{text}'"
            log.info(f"[green][+] Стоп-слово успешно перехвачено: '{matched}' для '{text[:40]}...'[/green]")
        else:
            assert not bool(matched), f"Ложное срабатывание стоп-слова '{matched}' для: '{text}'"
            log.info(f"[green][+] Чистый заказ без стоп-слов пропущен: '{text[:40]}...'[/green]")

    # 3. Тест базы данных
    db = OrderDatabase("data/test_workzilla.db")
    db.save_order(
        order_id="test_101",
        title="Тестовый парсер",
        description="Собрать данные",
        price=500.0,
        status="PENDING",
        feasible=True,
        confidence=0.95,
        llm_reason="Python scripting",
        proposal_message="Готов выполнить парсинг на Python."
    )
    assert db.order_exists("test_101") == True, "Заказ не найден в БД!"
    order = db.get_order("test_101")
    assert order["price"] == 500.0, "Неверная цена заказа!"
    db.mark_applied("test_101")
    order = db.get_order("test_101")
    assert order["status"] == "APPLIED", "Статус не обновился на APPLIED!"
    log.info("[green][+] Тест базы данных SQLite пройден успешно![/green]")

    # 4. Тест LLM Evaluator (если задан GEMINI_API_KEY)
    if config.gemini_api_key:
        log.info(f"[cyan]Тестирование Gemini API ({config.gemini.model}) через Proxy ({config.proxy_url})...[/cyan]")
        evaluator = GeminiEvaluator(config)

        sample_orders = [
            {
                "title": "Написать скрипт для парсинга цен с сайта в Excel",
                "desc": "Нужен парсер на Python, который собирает цены и сохраняет в CSV/XLSX.",
                "price": 1000.0,
                "expected_feasible": True
            },
            {
                "title": "Срочно сходить в налоговую и подать заявление лично",
                "desc": "Нужно присутствовать в отделении г. Москва и подписать бланк.",
                "price": 2000.0,
                "expected_feasible": False
            }
        ]

        table = Table(title="Результаты оценки синтетических заказов через Gemini", show_header=True)
        table.add_column("Заказ", style="cyan")
        table.add_column("Feasible", style="bold")
        table.add_column("Confidence", style="magenta")
        table.add_column("Причина / Отклик", style="white")

        for sample in sample_orders:
            res = await evaluator.evaluate_order(sample["title"], sample["desc"], sample["price"])
            status_color = "green" if res.feasible else "red"
            msg_or_reason = res.proposal_message if res.feasible else res.reason
            table.add_row(
                sample["title"][:40],
                f"[{status_color}]{res.feasible}[/{status_color}]",
                f"{res.confidence:.2f}",
                f"{msg_or_reason[:70]}..."
            )
            # Проверка лимита символов
            if res.feasible:
                assert len(res.proposal_message) <= config.filters.max_proposal_chars, "Превышен лимит символов отклика!"

        console.print(table)
        log.info("[green][+] Тест LLM Evaluator пройден успешно![/green]")
    else:
        log.info("[yellow]GEMINI_API_KEY не установлен в .env, тест реального вызова API пропущен.[/yellow]")

    console.print("\n[bold green]Все базовые тесты компонентов завершены успешно![/bold green]\n")

if __name__ == "__main__":
    asyncio.run(run_tests())
