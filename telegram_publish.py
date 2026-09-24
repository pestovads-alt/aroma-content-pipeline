"""
Публикация готового черновика из posts/ в Telegram-канал через Bot API.

Вызывается только из веб-интерфейса по кнопке «Опубликовать» (после
подтверждения пользователем) или вручную из терминала:

    python telegram_publish.py posts/2026-09-22-roman-chamomile-resource.md --dry-run
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

TELEGRAM_MESSAGE_LIMIT = 4096


def split_frontmatter(content: str) -> tuple[dict, str]:
    """Делит файл на YAML-фронтматтер (плоский key: value) и тело поста."""
    meta: dict = {}
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip().strip('"')
            return meta, parts[2].strip()
    return meta, content.strip()


def join_frontmatter(meta: dict, body: str) -> str:
    lines = [f'{k}: "{v}"' if k == "oil" else f"{k}: {v}" for k, v in meta.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body.strip() + "\n"


def _send(token: str, chat_id: str, text: str, parse_mode: str | None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15
    )
    return response.json()


def publish_to_telegram(token: str, chat_id: str, text: str) -> dict:
    """
    Сначала пробует parse_mode="Markdown" (посты размечены *жирным* и _курсивом_).
    Если Telegram не распарсил разметку — отправляет обычным текстом, а не падает молча.
    """
    result = _send(token, chat_id, text, parse_mode="Markdown")
    if result.get("ok"):
        return {"status": "ok", "message_id": result["result"]["message_id"],
                "formatting": "markdown"}

    if "can't parse entities" in (result.get("description") or "").lower():
        result = _send(token, chat_id, text, parse_mode=None)
        if result.get("ok"):
            return {"status": "ok", "message_id": result["result"]["message_id"],
                    "formatting": "plain (markdown не распарсился)"}

    return {"status": "error", "error": result.get("description", "Unknown error")}


def publish_file(path: str) -> dict:
    """Публикует файл черновика и помечает его во фронтматтере как опубликованный."""
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"status": "error", "error": "TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID должны быть в .env"}

    with open(path, encoding="utf-8") as f:
        meta, body = split_frontmatter(f.read())

    if meta.get("status") == "published":
        return {"status": "error", "error": "Этот пост уже опубликован"}
    if len(body) > TELEGRAM_MESSAGE_LIMIT:
        return {"status": "error",
                "error": f"Текст длиннее лимита Telegram ({len(body)} > {TELEGRAM_MESSAGE_LIMIT})"}

    result = publish_to_telegram(token, chat_id, body)
    if result["status"] == "ok":
        meta["status"] = "published"
        meta["message_id"] = result["message_id"]
        with open(path, "w", encoding="utf-8") as f:
            f.write(join_frontmatter(meta, body))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown_path", help="Путь к файлу в posts/")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать, что будет отправлено, без реальной отправки")
    args = parser.parse_args()

    if not os.path.exists(args.markdown_path):
        sys.exit(f"❌ Файл не найден: {args.markdown_path}")

    if args.dry_run:
        with open(args.markdown_path, encoding="utf-8") as f:
            print(split_frontmatter(f.read())[1])
        return

    result = publish_file(args.markdown_path)
    if result["status"] == "ok":
        print(f"✅ Опубликовано! Message ID: {result['message_id']} ({result['formatting']})")
    else:
        sys.exit(f"❌ Ошибка: {result['error']}")


if __name__ == "__main__":
    main()
