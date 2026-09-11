import json
import re
from typing import Optional, Dict, Any
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

class GeminiEvaluator:
    """Оценщик заказов на базе Google Gemini с поддержкой Proxy."""

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
- Требуется регистрация с привязкой реального номера телефона, получение SMS, верификация личности по паспорту, оформление банковских карт.
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

    async def evaluate_order(self, title: str, description: str, price: float = 0.0) -> EvaluationResult:
        """Оценить заказ через Gemini API с прокси."""
        if not self.api_key:
            log.warning("[yellow]GEMINI_API_KEY не задан! Заказ помечен как невыполнимый.[/yellow]")
            return EvaluationResult(
                feasible=False,
                confidence=0.0,
                reason="Отсутствует GEMINI_API_KEY",
                proposal_message=""
            )

        # Подставляем ограничение по символам в системный промпт
        system_instruction = self.SYSTEM_PROMPT.format(max_chars=self.max_chars)

        user_content = f"""Заказ с биржи:
Заголовок: {title}
Описание: {description}
Бюджет: {price} руб."""

        # Формируем payload для Gemini API
        payload = {
            "contents": [
                {
                    "parts": [{"text": user_content}]
                }
            ],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "temperature": self.config.gemini.temperature,
                "maxOutputTokens": self.config.gemini.max_tokens,
                "responseMimeType": "application/json"
            }
        }

        # URL Gemini API
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        # Настраиваем прокси для httpx
        proxies = self.proxy_url if self.proxy_url else None
        timeout = httpx.Timeout(self.config.gemini.timeout_seconds, connect=15.0)

        try:
            async with httpx.AsyncClient(proxy=proxies, timeout=timeout) as client:
                response = await client.post(endpoint, json=payload)

                if response.status_code != 200:
                    log.error(f"[red]Ошибка Gemini API ({response.status_code}): {response.text}[/red]")
                    return EvaluationResult(
                        feasible=False,
                        confidence=0.0,
                        reason=f"API Error {response.status_code}",
                        proposal_message=""
                    )

                data = response.json()
                # Извлекаем текст ответа
                candidates = data.get("candidates", [])
                if not candidates:
                    return EvaluationResult(feasible=False, confidence=0.0, reason="Нет кандидатов в ответе Gemini")

                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                
                # Очищаем от возможных markdown обрамлений ```json ... ```
                raw_text = re.sub(r"^```json\s*", "", raw_text.strip(), flags=re.MULTILINE)
                raw_text = re.sub(r"^```\s*$", "", raw_text.strip(), flags=re.MULTILINE)

                parsed = json.loads(raw_text)

                result = EvaluationResult(
                    feasible=bool(parsed.get("feasible", False)),
                    confidence=float(parsed.get("confidence", 0.0)),
                    reason=str(parsed.get("reason", "")),
                    proposal_message=str(parsed.get("proposal_message", "")).strip()
                )

                # Проверяем лимит длины отклика
                if len(result.proposal_message) > self.max_chars:
                    result.proposal_message = result.proposal_message[:self.max_chars].rstrip() + "..."

                return result

        except httpx.ProxyError as e:
            log.error(f"[red]Ошибка соединения с Proxy ({self.proxy_url}): {e}[/red]")
            return EvaluationResult(feasible=False, confidence=0.0, reason="Proxy connection error")
        except Exception as e:
            log.error(f"[red]Исключение при оценке заказа через Gemini: {e}[/red]")
            return EvaluationResult(feasible=False, confidence=0.0, reason=str(e))
