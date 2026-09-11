import asyncio
import re
import sys
import subprocess
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright, BrowserContext, Page, ElementHandle
from src.config import AppConfig
from src.logger import log

class WorkzillaOrder:
    """Представление заказа с Work-zilla."""
    def __init__(self, order_id: str, title: str, description: str, price: float, raw_element: Optional[ElementHandle] = None):
        self.order_id = order_id
        self.title = title
        self.description = description
        self.price = price
        self.raw_element = raw_element

    def __repr__(self):
        return f"<WorkzillaOrder id={self.order_id} price={self.price} title='{self.title[:30]}...'>"

class WorkzillaBrowserClient:
    """Клиент автоматизации браузера для биржи Work-zilla."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._is_connected = False

    async def start(self) -> bool:
        """Инициализация браузера и подключение к сессии."""
        self.playwright = await async_playwright().start()

        if self.config.browser_mode == "cdp":
            log.info(f"[cyan]Подключение к Chrome через CDP ({self.config.cdp_url})...[/cyan]")
            try:
                browser = await self.playwright.chromium.connect_over_cdp(self.config.cdp_url)
                if not browser.contexts:
                    log.error("[red]Не найдено открытых контекстов в Chrome через CDP.[/red]")
                    return False
                self.context = browser.contexts[0]
                
                # Ищем вкладку с work-zilla или создаем новую
                for p in self.context.pages:
                    if "work-zilla.com" in p.url:
                        self.page = p
                        log.info(f"[green]Найдена открытая вкладка Work-zilla: {p.url}[/green]")
                        break

                if not self.page:
                    self.page = await self.context.new_page()
                    await self.page.goto("https://client.work-zilla.com/freelancer", wait_until="networkidle")

                self._is_connected = True
                return True
            except Exception as e:
                log.error(f"[red]Не удалось подключиться через CDP: {e}[/red]")
                log.info("[yellow]Подсказка: запустите Chrome с флагом --remote-debugging-port=9222[/yellow]")
                return False
        else:
            log.info(f"[cyan]Запуск Playwright в режиме persistent context (headless={self.config.headless})...[/cyan]")
            user_data_dir = "data/browser_profile"
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=self.config.headless,
                    viewport={"width": 1280, "height": 800},
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
            except Exception as e:
                err_text = str(e)
                if "Executable doesn't exist" in err_text or "playwright install" in err_text:
                    log.warning("[yellow]Бинарные файлы Chromium не найдены. Выполняю автоматическую установку Playwright Chromium...[/yellow]")
                    try:
                        # Установка chromium
                        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                        log.info("[green]Chromium успешно установлен. Повторный запуск...[/green]")
                    except Exception as install_err:
                        log.error(f"[red]Ошибка при установке Chromium: {install_err}[/red]")
                        raise e

                    # Повторная попытка после установки
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        headless=self.config.headless,
                        viewport={"width": 1280, "height": 800},
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                    )
                else:
                    raise e
            
            # Если переданы куки через переменную окружения (актуально для серверов/BotHost)
            if self.config.workzilla_cookies:
                try:
                    import json
                    cookies_data = []
                    raw = self.config.workzilla_cookies.strip()
                    if raw.startswith("[") and raw.endswith("]"):
                        cookies_data = json.loads(raw)
                    else:
                        # Формат cookie header "name1=val1; name2=val2"
                        for item in raw.split(";"):
                            if "=" in item:
                                k, v = item.strip().split("=", 1)
                                cookies_data.append({
                                    "name": k.strip(),
                                    "value": v.strip(),
                                    "domain": ".work-zilla.com",
                                    "path": "/"
                                })
                    if cookies_data:
                        await self.context.add_cookies(cookies_data)
                        log.info(f"[green]Успешно загружено куки Work-zilla ({len(cookies_data)} шт.)[/green]")
                except Exception as e:
                    log.error(f"[red]Ошибка при парсинге WORKZILLA_COOKIES: {e}[/red]")

            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto("https://client.work-zilla.com/freelancer")
            self._is_connected = True
            return True

    async def close(self):
        """Закрытие соединений."""
        if self.playwright:
            await self.playwright.stop()
            self._is_connected = False

    async def ensure_new_tab_active(self):
        """Проверка вкладки 'Новые' выполняется только один раз при старте."""
        pass

    async def fetch_orders(self) -> List[WorkzillaOrder]:
        """Парсинг карточек новых заказов из ленты /freelancer."""
        if not self.page:
            return []

        orders: List[WorkzillaOrder] = []
        seen_titles = set()
        seen_ids = set()

        try:
            # 1. Поиск карточек заказов по стандартным классам Workzilla
            card_elements = await self.page.query_selector_all(
                ".order-card, .vacancy-item, [data-order-id], [class*='order_card'], [class*='OrderItem'], [class*='order-item'], [class*='taskItem'], [class*='vacancyItem']"
            )

            # 2. Если по классам не найдено — ищем по чекбоксам на карточках
            if not card_elements:
                checkboxes = await self.page.query_selector_all("input[type='checkbox'], [role='checkbox'], [class*='checkbox']")
                for cb in checkboxes:
                    try:
                        is_select_all = await cb.evaluate("""el => {
                            const p = el.closest('div') || el.parentElement;
                            return p && p.innerText && p.innerText.includes('Выбрать всё');
                        }""")
                        if is_select_all:
                            continue
                        card_h = await cb.evaluate_handle("""el => {
                            return el.closest('[class*=\"item\"], [class*=\"card\"], [class*=\"order\"], [class*=\"vacancy\"], article, li') || el.parentElement.parentElement;
                        }""")
                        el = card_h.as_element()
                        if el and el not in card_elements:
                            card_elements.append(el)
                    except Exception:
                        continue

            for card_el in card_elements:
                try:
                    raw_text = await card_el.inner_text()
                    text_clean = raw_text.replace("\xa0", " ").replace("&nbsp;", " ")
                    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]

                    # Пропускаем заголовки секций "Интересные задания" и "Новые задания"
                    if not lines or lines[0] in ("Интересные задания", "Новые задания", "Выбрать всё"):
                        continue

                    title = lines[0]
                    # Дедупликация вложенных родительских блоков
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)

                    description = "\n".join(lines[1:]) if len(lines) > 1 else ""

                    # Поиск ID заказа
                    order_id = await card_el.get_attribute("data-order-id") or await card_el.get_attribute("id")
                    if not order_id:
                        link_el = await card_el.query_selector("a[href*='/order/'], a[href*='/task/'], a[href*='/vacancy/']")
                        if link_el:
                            href = await link_el.get_attribute("href") or ""
                            m = re.search(r"/(?:order|task|vacancy)/([0-9a-zA-Z_-]+)", href)
                            if m:
                                order_id = m.group(1)

                    # Парсинг цены (число рядом с таймером '6ч 0м 1000' или отдельная строка числа)
                    price = 0.0
                    price_match = re.search(r'(?:(?:\d+\s*[дd]\s*)?(?:\d+\s*[чh]\s*)?(?:\d+\s*[мm]\s*)?)\s*(\d{3,6})\b', text_clean)
                    if price_match:
                        price = float(price_match.group(1))
                    else:
                        for l in lines:
                            cl = re.sub(r"[^\d]", "", l)
                            if cl and 100 <= int(cl) <= 500000:
                                price = float(cl)
                                break

                    if not order_id:
                        import hashlib
                        order_id = hashlib.md5(f"{title}_{price}".encode()).hexdigest()[:12]

                    if order_id in seen_ids:
                        continue
                    seen_ids.add(order_id)

                    orders.append(WorkzillaOrder(
                        order_id=order_id,
                        title=title,
                        description=description,
                        price=price,
                        raw_element=card_el
                    ))
                except Exception as card_err:
                    log.debug(f"Ошибка парсинга отдельной карточки: {card_err}")
                    continue

        except Exception as e:
            log.error(f"[red]Ошибка при сканировании заказов со страницы: {e}[/red]")

        return orders

    async def hide_order(self, order: WorkzillaOrder) -> bool:
        """Скрыть заказ в интерфейсе Work-zilla (удалить из ленты)."""
        if not self.page or not order.raw_element:
            return False

        try:
            # Ищем кнопку "Скрыть" или иконку крестика в карточке
            hide_btn = await order.raw_element.query_selector(
                "button[title*='Скрыть'], button:has-text('Скрыть'), [class*='hide'], [class*='dismiss'], [class*='close']"
            )
            if hide_btn:
                await hide_btn.click()
                log.info(f"[dim]Заказ #{order.order_id} скрыт из ленты.[/dim]")
                return True
            return False
        except Exception as e:
            log.debug(f"Не удалось скрыть заказ #{order.order_id}: {e}")
            return False

    async def apply_order(self, order: WorkzillaOrder, proposal_text: str, dry_run: bool = True) -> bool:
        """Откликнуться на заказ с сопроводительным письмом."""
        if dry_run:
            log.info(f"[yellow][DRY-RUN] Имитация отклика на заказ #{order.order_id}: '{order.title}' ({order.price:.0f} руб.)[/yellow]")
            log.info(f"[italic]Текст отклика:[/italic] {proposal_text}")
            return True

        if not self.page or not order.raw_element:
            return False

        try:
            # В Work-zilla кнопка 'Откликнуться' появляется при клике по карточке заказа
            apply_btn = await order.raw_element.query_selector(
                "button:has-text('Откликнуться'), button:has-text('Подать заявку'), a:has-text('Откликнуться')"
            )
            if not apply_btn:
                # Кликаем по карточке, чтобы открыть детали задания
                title_el = await order.raw_element.query_selector("h1, h2, h3, h4, a, [class*='title'], [class*='name']")
                if title_el:
                    await title_el.click()
                else:
                    await order.raw_element.click()
                await asyncio.sleep(1.5)

                # Ищем кнопку "Откликнуться" в открывшемся окне/панели
                apply_btn = await self.page.query_selector(
                    "button:has-text('Откликнуться'), button:has-text('Подать заявку'), a:has-text('Откликнуться')"
                )

            if not apply_btn:
                log.warning(f"[yellow]Кнопка 'Откликнуться' не найдена для #{order.order_id}[/yellow]")
                return False

            await apply_btn.click()
            await asyncio.sleep(1.0)

            # Поле ввода комментария
            textarea = await self.page.query_selector("textarea[placeholder*='комментарий'], textarea[placeholder*='сообщение'], textarea")
            if not textarea:
                log.error(f"[red]Поле ввода отклика не найдено для #{order.order_id}[/red]")
                return False

            # Человекоподобный ввод текста
            await textarea.fill(proposal_text)
            await asyncio.sleep(1.5)

            # Кнопка подтверждения отправки
            submit_btn = await self.page.query_selector(
                "button:has-text('Отправить'), button:has-text('Подтвердить'), button:has-text('Согласен')"
            )
            if submit_btn:
                await submit_btn.click()
                log.info(f"[green]Успешно отправлен отклик на заказ #{order.order_id}![/green]")
                return True
            else:
                log.warning("[yellow]Кнопка подтверждения отправки отклика не найдена.[/yellow]")
                return False

        except Exception as e:
            log.error(f"[red]Ошибка при отправке отклика на #{order.order_id}: {e}[/red]")
            return False
