import json
import subprocess
import sys
from playwright.sync_api import sync_playwright

def copy_to_clipboard(text: str):
    """Копирование строки в буфер обмена Windows через PowerShell."""
    try:
        process = subprocess.Popen(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], stdin=subprocess.PIPE)
        process.communicate(input=text.encode("utf-8"))
        return True
    except Exception:
        return False

def main():
    print("=" * 60)
    print("Запуск браузера для входа на Work-zilla...")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("\nПереход на страницу входа Work-zilla...")
        page.goto("https://client.work-zilla.com/freelancer")

        print("\n" + "!" * 60)
        print("ДЕЙСТВИЕ ДЛЯ ВАС:")
        print("1. В открывшемся окне браузера войдите в свой аккаунт Work-zilla.")
        print("2. Убедитесь, что открылась лента заказов.")
        print("3. Вернитесь в эту консоль и нажмите ENTER.")
        print("!" * 60 + "\n")

        input("Нажмите [ENTER] после того, как успешно вошли в аккаунт...")

        # Получаем абсолютно все куки (включая HttpOnly)
        cookies = context.cookies()
        browser.close()

        if not cookies:
            print("\n[ОШИБКА] Куки не найдены! Возможно, вы не выполнили вход.")
            return

        cookies_json = json.dumps(cookies, ensure_ascii=False)

        # Копируем в буфер обмена
        copied = copy_to_clipboard(cookies_json)

        print("\n" + "=" * 60)
        print("УСПЕХ! Куки успешно получены!")
        if copied:
            print("[+] Строка УЖЕ СКОПИРОВАНА в ваш буфер обмена (Ctrl + V).")
        print("=" * 60)
        print("\nЗначение для переменной WORKZILLA_COOKIES на BotHost:\n")
        print(cookies_json)
        print("\n" + "=" * 60)
        print("Вставьте это значение в поле WORKZILLA_COOKIES на BotHost.")
        print("=" * 60)

if __name__ == "__main__":
    main()
