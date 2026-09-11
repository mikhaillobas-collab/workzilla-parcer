import asyncio
import os
import re
import sys
from rich.console import Console
from rich.table import Table
from playwright.async_api import async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

async def inspect():
    console.print("\n[bold cyan]=== Диагностика и просмотр Work-zilla ===[/bold cyan]\n")
    
    async with async_playwright() as p:
        console.print("[yellow]Запуск браузера с графическим окном для визуальной проверки...[/yellow]")
        
        # Запускаем браузер в видимом режиме (headless=False), чтобы видеть глазами
        context = await p.chromium.launch_persistent_context(
            user_data_dir="data/browser_profile",
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"]
        )

        page = context.pages[0] if context.pages else await context.new_page()
        
        console.print("[cyan]Переход на https://client.work-zilla.com/freelancer...[/cyan]")
        await page.goto("https://client.work-zilla.com/freelancer", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 1. Проверяем вкладки (Новые, Открытые, История)
        console.print("[bold]Поиск вкладок на странице...[/bold]")
        tabs = await page.query_selector_all("button, a, div[role='tab'], li")
        tab_names = []
        new_tab_el = None
        for t in tabs:
            txt = (await t.inner_text()).strip()
            if txt in ("Новые", "Открытые", "История"):
                tab_names.append(txt)
                if txt == "Новые" and not new_tab_el:
                    new_tab_el = t

        console.print(f"[green]Найдены вкладки:[/green] {set(tab_names)}")

        # Если нашли вкладку "Новые" — кликаем на неё для гарантии
        if new_tab_el:
            console.print("[cyan]Кликаем на вкладку 'Новые' для гарантированного открытия свежих заданий...[/cyan]")
            try:
                await new_tab_el.click()
                await asyncio.sleep(2)
            except Exception as e:
                console.print(f"[red]Не удалось кликнуть по вкладке: {e}[/red]")

        # 2. Ищем все кнопки "Откликнуться"
        apply_buttons = await page.query_selector_all(
            "button:has-text('Откликнуться'), button:has-text('Подать заявку'), a:has-text('Откликнуться')"
        )
        console.print(f"[bold green]Найдено доступных кнопок 'Откликнуться' в разделе 'Новые': {len(apply_buttons)}[/bold green]\n")

        table = Table(title="Реальные заказы в разделе 'Новые'", show_header=True)
        table.add_column("№", style="dim", width=4)
        table.add_column("Цена", style="bold green", width=12)
        table.add_column("Заголовок заказа", style="cyan")
        table.add_column("Подходит под >= 1000 руб?", style="bold")

        count = 0
        for btn in apply_buttons:
            count += 1
            # Получаем родительский контейнер карточки
            card = await btn.evaluate_handle(
                "el => el.closest('[data-order-id]') || el.closest('.order-card') || el.closest('[class*=\"order\"]') || el.closest('[class*=\"vacancy\"]') || el.closest('article') || el.closest('li') || el.parentElement.parentElement"
            )
            
            raw_text = await card.as_element().inner_text()
            text_clean = raw_text.replace("\xa0", " ").replace("&nbsp;", " ")
            lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
            title = lines[0] if lines else "Без названия"

            # Ищем цену
            price = 0.0
            price_match = re.search(r"(\d[\d\s]{0,8})\s*(?:₽|руб\.?|р\.?)", text_clean, re.IGNORECASE)
            if price_match:
                clean_digits = re.sub(r"[^\d]", "", price_match.group(1))
                if clean_digits:
                    price = float(clean_digits)

            match_1000 = "[green]ДА (>= 1000 руб)[/green]" if price >= 1000 else f"[yellow]НЕТ ({price} руб)[/yellow]"

            table.add_row(
                str(count),
                f"{price:.0f} руб." if price > 0 else "Не распознана",
                title[:60],
                match_1000
            )

        console.print(table)

        # Делаем скриншот для подтверждения
        os.makedirs("data", exist_ok=True)
        screenshot_path = "data/workzilla_view.png"
        await page.screenshot(path=screenshot_path)
        console.print(f"\n[green]Снимок экрана сохранен в: {screenshot_path}[/green]")
        console.print("[dim]Окно браузера открыто. Вы можете посмотреть его своими глазами. Нажмите Ctrl+C в терминале для закрытия.[/dim]\n")

        # Оставляем открытым на 30 секунд для просмотра
        await asyncio.sleep(30)
        await context.close()

if __name__ == "__main__":
    asyncio.run(inspect())
