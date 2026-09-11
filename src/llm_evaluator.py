import asyncio
import json
import re
from typing import Optional, Dict, Any, List
import httpx
from pydantic import BaseModel, Field

from src.config import AppConfig
from src.logger import log

class EvaluationResult(BaseModel):
    """Результат оценки заказа языковой моделью."""
    feasible: bool = Field(description="Выполнима ли задача исключительно силами LLM онлайн")
    confidence: float = Field(default=0.0, description="Уверенность модели от 0.0 до 1.0")
    reason: str = Field(default="", description="Краткое объяснение вердикта")
    proposal_message: str = Field(default="", description="Текст сопроводительного отклика")
    is_api_error: bool = Field(default=False, description="Признак сбоя или исчерпания лимита API")

class GeminiEvaluator:
    """Оценщик заказов на базе Google Gemini с поддержкой Proxy и retry."""

    FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.6-flash"]

    SYSTEM_PROMPT = """Ты — строгий эксперт по оценке задач для фрилансера.
Твоя цель — определить, может ли предложенная задача быть выполнена ИСКЛЮЧИТЕЛЬНО силами языковой модели (LLM) онлайн, без любого ручного участия человека, без оффлайна и без использования личных аккаунтов.

Критерии ВЫПОЛНИМОСТИ (feasible = true):
- Написание, рерайт, перевод, редактура текстов, статей, постов, карточек товаров.
- Составление контент-планов, сценариев, описаний, документации.
- Написание программного кода, скриптов на Python/JS, SQL-запросов, формул Excel/Google Sheets, HTML/CSS верстка.
- Анализ данных, суммаризация, парсинг данных по готовым алгоритмам, регулярные выражения.

Критерии НЕВЫПОЛНИМОСТИ (feasible = false):
- Требуется физическое присутствие или звонки по телефону/созвоны.
- Требуется публикация отзыва, комментариев со своего личного аккаунта (Авито, Яндекс.Карты, 2ГИС, Google Maps, соцсети).
- Требуется регистрация с привязкой реального номера телефона, получение SMS, верификация личности по паспорту, оформление банковских карт, регистрация в БК/букмекерских конторах.
- Требуется ручной труд в закрытых приложениях (скачать приложение на смартфон, кликать рекламу, монотонное заполнение форм без API).
- Требуется живое портфолио человека с видеоподтверждением или созвон с заказчиком.

Если feasible == true:
Сгенерируй короткий, уверенный сопроводительный отклик (proposal_message):
- Лимит: строго ДО {max_chars} символов!
- Вежливый, деловой тон без "воды".
- Обязательно укажи стек или подход (например: "Решу задачу скриптом на Python (Pandas/BeautifulSoup)", "Напишу качественный текст с глубоким анализом темы").
- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать "Я нейросеть", "Я искусственный интеллект", "Сгенерировано ботом". Заказчики бирж не принимают ботов.

Ответь СТРОГО в формате JSON без разметки markdown:
{{
  "feasible": boolean,
  "confidence": float,
  "reason": "короткое пояснение на русском языке",
  "proposal_message": "готовый текст отклика (или пустая строка, если feasible=false)"
}}"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.api_key = config.gemini_api_key
        self.model = config.gemini.model
        self.proxy_url = config.proxy_url
        self.max_chars = config.filters.max_proposal_chars

    async def _call_gemini_api(
        self, client: httpx.AsyncClient, model_name: str, payload: dict
    ) -> Optional[EvaluationResult]:
        """Одиночный вызов Gemini API с обработкой 429 и парсингом JSON."""
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
        
        max_attempts = 2
        for attempt in range(max_attempts):
            response = await client.post(endpoint, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return EvaluationResult(
                        feasible=False, confidence=0.0, reason="Нет кандидатов в ответе Gemini"
                    )

                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                raw_text = re.sub(r"^```json\s*", "", raw_text.strip(), flags=re.MULTILINE)
                raw_text = re.sub(r"^```\s*$", "", raw_text.strip(), flags=re.MULTILINE)

                parsed = json.loads(raw_text)
                proposal = str(parsed.get("proposal_message", "")).strip()
                if len(proposal) > self.max_chars:
                    proposal = proposal[:self.max_chars].rstrip() + "..."

                return EvaluationResult(
                    feasible=bool(parsed.get("feasible", False)),
                    confidence=float(parsed.get("confidence", 0.0)),
                    reason=str(parsed.get("reason", "")),
                    proposal_message=proposal,
                    is_api_error=False,
                )

            if response.status_code == 429:
                # Извлекаем время ожидания, указанное сервером Google
                delay = 15.0
                m = re.search(r'retry in (\d+(?:\.\d+)?)s', response.text)
                if m:
                    delay = min(60.0, float(m.group(1)) + 2.0)
                else:
                    m2 = re.search(r'"retryDelay":\s*"(\d+)s"', response.text)
                    if m2:
                        delay = min(60.0, float(m2.group(1)) + 2.0)

                log.warning(
                    f"[yellow]Лимит Gemini API для модели '{model_name}' (429). "
                    f"Пауза {delay:.0f} сек перед повтором...[/yellow]"
                )
                await asyncio.sleep(delay)
                continue

            # Для других ошибок (404, 400 и т.п.) не делаем retry
            log.error(f"[red]Ошибка Gemini API ({response.status_code}) для '{model_name}': {response.text[:200]}[/red]")
            return None

        return None

    async def evaluate_order(self, title: str, description: str, price: float = 0.0) -> EvaluationResult:
        """Оценить заказ через Gemini API с fallback-моделями и защитой от 429."""
        if not self.api_key:
            log.warning("[yellow]GEMINI_API_KEY не задан! Заказ помечен как невыполнимый.[/yellow]")
            return EvaluationResult(
                feasible=False,
                confidence=0.0,
                reason="Отсутствует GEMINI_API_KEY",
                proposal_message="",
                is_api_error=False,
            )

        system_instruction = self.SYSTEM_PROMPT.format(max_chars=self.max_chars)
        user_content = f"Заказ на бирже:\nЗаголовок: {title}\nОписание:\n{description}\nЦена: {price} руб."

        payload = {
            "contents": [{"parts": [{"text": user_content}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {
                "temperature": self.config.gemini.temperature,
                "maxOutputTokens": self.config.gemini.max_tokens,
                "responseMimeType": "application/json",
            },
        }

        # Модели для проверки: сначала основная, затем fallback (2.0-flash, 1.5-flash)
        candidate_models = [self.model]
        for fm in self.FALLBACK_MODELS:
            if fm not in candidate_models:
                candidate_models.append(fm)

        proxies = self.proxy_url if self.proxy_url else None
        timeout = httpx.Timeout(self.config.gemini.timeout_seconds, connect=15.0)

        try:
            async with httpx.AsyncClient(proxy=proxies, timeout=timeout) as client:
                for model_name in candidate_models:
                    res = await self._call_gemini_api(client, model_name, payload)
                    if res is not None:
                        return res
                    log.info(f"[cyan]Пробуем резервную модель Gemini...[/cyan]")

                # Если все модели вернули ошибку квоты
                return EvaluationResult(
                    feasible=False,
                    confidence=0.0,
                    reason="Лимит запросов Gemini исчерпан (429 Rate Limit)",
                    proposal_message="",
                    is_api_error=True,
                )

        except httpx.ProxyError as e:
            log.error(f"[red]Ошибка соединения с Proxy ({self.proxy_url}): {e}[/red]")
            return EvaluationResult(
                feasible=False, confidence=0.0, reason="Proxy connection error", is_api_error=True
            )
        except Exception as e:
            log.error(f"[red]Исключение при оценке заказа через Gemini: {e}[/red]")
            return EvaluationResult(
                feasible=False, confidence=0.0, reason=str(e), is_api_error=True
            )
