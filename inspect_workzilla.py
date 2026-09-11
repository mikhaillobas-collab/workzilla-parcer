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
    console.print("\n[bold cyan]=== Инспекция страницы заказов Work-zilla ===[/bold cyan]\n")
    
    async with async_playwright() as p:
        console.print("[yellow]Запуск браузера... Окно НЕ закроется само![/yellow]")
        
        # Запуск с сохранением профиля, чтобы не входить каждый раз заново
        context = await p.chromium.launch_persistent_context(
            user_data_dir="data/browser_profile",
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"]
        )

        page = context.pages[0] if context.pages else await context.new_page()
        
        console.print("[cyan]Переход на https://client.work-zilla.com/freelancer...[/cyan]")
        await page.goto("https://client.work-zilla.com/freelancer")
        await asyncio.sleep(2)

        # Проверяем, залогинены ли мы
        current_url = page.url
        body_text = await page.inner_text("body")

        if "Войти" in body_text or "зарегистрироваться" in body_text or "login" in current_url.lower():
            console.print("\n[bold yellow]" + "!" * 65 + "[/bold yellow]")
            console.print("[bold yellow]ВНИМАНИЕ: Вы еще не авторизованы в этом локальном браузере![/bold yellow]")
            console.print("[bold white]1. Прямо сейчас в открывшемся окне Chrome введите логин и пароль Work-zilla.[/bold white]")
            console.print("[bold white]2. Дождитесь, пока откроется лента заказов.[/bold white]")
            console.print("[bold white]3. Вернитесь в эту консоль и нажмите клавишу ENTER.[/bold white]")
            console.print("[bold yellow]" + "!" * 65 + "[/bold yellow]\n")

            # Ждем ввода от пользователя
            await asyncio.to_thread(input, "После входа в аккаунт нажмите [ENTER] здесь, в консоли...")
            await asyncio.sleep(2)

        # 1. Проверяем вкладки (Новые, Открытые, История)
        console.print("\n[bold]Поиск вкладки 'Новые'...[/bold]")
        tabs = await page.query_selector_all("button, a, div[role='tab'], li, [class*='tab']")
        new_tab_el = None
        for t in tabs:
            txt = (await t.inner_text()).strip()
            if txt == "Новые":
                new_tab_el = t
                break

        if new_tab_el:
            console.print("[bold green]Кликаем на вкладку 'Новые'![/bold green]")
            try:
                await new_tab_el.click()
                await asyncio.sleep(2)
            except Exception as e:
                console.print(f"[red]Ошибка при клике на вкладку: {e}[/red]")
        else:
            console.print("[yellow]Вкладка 'Новые' не найдена отдельной кнопкой (возможно, она уже активна).[/yellow]")

        # 2. Ищем кнопки "Откликнуться"
        apply_buttons = await page.query_selector_all(
            "button:has-text('Откликнуться'), a:has-text('Откликнуться'), button:has-text('Подать заявку')"
        )
        console.print(f"\n[bold green]Найдено активных кнопок 'Откликнуться' в разделе 'Новые': {len(apply_buttons)}[/bold green]\n")

        table = Table(title="Реальные заказы во вкладке 'Новые'", show_header=True)
        table.add_column("№", style="dim", width=4)
        table.add_column("Цена", style="bold green", width=12)
        table.add_column("Заголовок заказа", style="cyan")
        table.add_column("Фильтр >= 1000 руб?", style="bold")

        count = 0
        seen_titles = set()
        for btn in apply_buttons:
            card = await btn.evaluate_handle(
                "el => el.closest('[data-order-id]') || el.closest('.order-card') || el.closest('[class*=\"order\"]') || el.closest('[class*=\"vacancy\"]') || el.closest('article') || el.closest('li') || el.parentElement.parentElement"
            )
            card_el = card.as_element()
            if not card_el:
                continue

            raw_text = await card_el.inner_text()
            text_clean = raw_text.replace("\xa0", " ").replace("&nbsp;", " ")
            lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
            title = lines[0] if lines else "Без названия"

            if title in seen_titles:
                continue
            seen_titles.add(title)
            count += 1

            # Поиск цены
            price = 0.0
            price_match = re.search(r"(\d[\d\s]{0,8})\s*(?:₽|руб\.?|р\.?)", text_clean, re.IGNORECASE)
            if price_match:
                clean_digits = re.sub(r"[^\d]", "", price_match.group(1))
                if clean_digits:
                    price = float(clean_digits)

            match_1000 = "[green]ДА (>= 1000 руб)[/green]" if price >= 1000 else f"[yellow]НЕТ ({price:.0f} руб)[/yellow]"

            table.add_row(
                str(count),
                f"{price:.0f} руб." if price > 0 else "Не распознана",
                title[:65],
                match_1000
            )

        console.print(table)

        # Скриншот
        os.makedirs("data", exist_ok=True)
        screenshot_path = "data/workzilla_view.png"
        await page.screenshot(path=screenshot_path)
        console.print(f"\n[green]✓ Актуальный снимок экрана сохранен в: {screenshot_path}[/green]")

        console.print("\n[bold cyan]" + "=" * 65 + "[/bold cyan]")
        console.print("[bold cyan]Окно браузера открыто. Вы можете изучать его сколько угодно.[/bold cyan]")
        console.print("[bold cyan]Когда закончите просмотр, нажмите [ENTER] в этой консоли, чтобы закрыть его.[/bold cyan]")
        console.print("[bold cyan]" + "=" * 65 + "[/bold cyan]\n")

        await asyncio.to_thread(input, "Нажмите [ENTER] для закрытия браузера...")
        await context.close()

if __name__ == "__main__":
    asyncio.run(inspect())
