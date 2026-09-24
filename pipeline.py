"""
Запуск конвейера из 4 агентов через Claude Code в headless-режиме (`claude -p`).

Работает на подписке, под которой выполнен вход в Claude Code (например, Pro) —
отдельный API-ключ не нужен. Прогресс по шагам берётся из потока stream-json:
каждый вызов субагента виден как tool_use с его именем.
"""

import json
import re
import subprocess
import threading
import time
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"
RUNS_DIR = ROOT / "runs"
RUN_TIMEOUT_SEC = 30 * 60

STEPS = [
    ("aroma-research-analyzer", "Разбор исследования"),
    ("aroma-fact-checker", "Проверка фактов"),
    ("aroma-content-writer", "Текст поста"),
    ("aroma-publisher", "Финальная проверка и оформление"),
]

# Bash не разрешён: агенты физически не могут отправить что-то в Telegram,
# публикация возможна только кнопкой в веб-интерфейсе.
ALLOWED_TOOLS = "Skill Agent Task Read Write Edit Glob Grep WebFetch WebSearch"

RESULT_CONTRACT = """
В самом конце ответа выведи ровно один блок ```json с итогом прогона:
{
  "status": "PASS | FAIL | INSUFFICIENT_DATA | no_natural_angle | pipeline_error",
  "saved_to": "posts/....md или null",
  "oil": "масло или null",
  "evidence_level": "уровень доказательности или null",
  "steps": [{"agent": "имя агента", "result": "вердикт/статус шага", "note": "одна строка"}],
  "issues": ["конкретные проблемы, если есть"],
  "message": "одна-две фразы для автора: что получилось и что делать дальше"
}
Ничего не публикуй и никуда не отправляй — черновик ждёт решения автора в веб-интерфейсе.
""".strip()

jobs: dict[str, dict] = {}
_lock = threading.Lock()


def build_prompt(source: str, focus: str) -> str:
    focus_line = f"\nФокус поста от автора: {focus}" if focus else ""
    return f"""Запусти скилл aroma-content-pipeline для входа ниже. Сегодня {date.today().isoformat()}.
Промежуточные JSON каждого шага сохрани в posts/.work/<slug>/ (analyzer.json, fact-checker.json, writer.json) —
они понадобятся, если автор вернёт черновик на доработку.

Вход (ссылка на исследование, текст или название масла):
{source}{focus_line}

{RESULT_CONTRACT}"""


def build_revision_prompt(draft_path: str, feedback: str) -> str:
    return f"""Автор вернул черновик {draft_path} на доработку. Замечания автора:
{feedback}

Промежуточные JSON этого поста лежат в posts/.work/ (папка по slug из имени файла).
Передай aroma-content-writer текущий текст черновика, замечания автора и проверенные факты/вердикт,
затем прогони результат через aroma-publisher и перезапиши тот же файл {draft_path}.
Не добавляй факты, которых нет в JSON fact-checker, — если замечание этого требует, верни FAIL с объяснением.

{RESULT_CONTRACT}"""


def active_job() -> dict | None:
    with _lock:
        return next((j for j in jobs.values() if j["state"] == "running"), None)


def start_job(prompt: str, title: str, skip: tuple[str, ...] = ()) -> str:
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "title": title,
        "state": "running",
        "started": time.time(),
        "steps": {name: ("skipped" if name in skip else "pending") for name, _ in STEPS},
        "current": None,
        "result": None,
        "error": None,
    }
    with _lock:
        jobs[job_id] = job
    threading.Thread(target=_run, args=(job, prompt), daemon=True).start()
    return job_id


def _mark_step(job: dict, agent: str) -> None:
    if agent not in job["steps"]:
        return
    if job["current"] and job["current"] != agent:
        job["steps"][job["current"]] = "done"
    job["steps"][agent] = "running"
    job["current"] = agent


def _handle_event(job: dict, event: dict) -> None:
    if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                _mark_step(job, block.get("input", {}).get("subagent_type", ""))
    elif event.get("type") == "result":
        # итог может прийти несколькими сообщениями — копим все, JSON ищем с конца
        job["final_text"] = job.get("final_text", "") + "\n" + (event.get("result") or "")


def parse_result(text: str) -> dict | None:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    for raw in reversed(blocks):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None


def _run(job: dict, prompt: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"{time.strftime('%Y-%m-%d_%H-%M')}_{job['id']}.jsonl"
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", "sonnet",
        "--allowedTools", ALLOWED_TOOLS,
        "--permission-mode", "acceptEdits",
    ]
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            timer = threading.Timer(RUN_TIMEOUT_SEC, proc.kill)
            timer.start()
            for line in proc.stdout:
                log.write(line)
                try:
                    _handle_event(job, json.loads(line))
                except json.JSONDecodeError:
                    job["error"] = line.strip()  # не-JSON вывод CLI — обычно сообщение об ошибке
            proc.wait()
            timer.cancel()
    except FileNotFoundError:
        job.update(state="error", error="Не найдена команда claude — установите Claude Code и войдите в аккаунт")
        return

    job["log"] = str(log_path.relative_to(ROOT))
    text = job.get("final_text", "")
    result = parse_result(text)
    if result is None:
        job.update(state="error",
                   error=job.get("error") or "Конвейер не вернул итог. Подробности — в логе прогона.",
                   raw=text[-3000:])
        return

    if job["current"]:
        job["steps"][job["current"]] = "done" if result.get("status") == "PASS" else "stopped"
    job.update(state="done", result=result, finished=time.time())
    save_run(job)


def save_run(job: dict) -> None:
    """Сохраняет итог прогона рядом с черновиком — история проверки переживает перезапуск."""
    saved_to = (job.get("result") or {}).get("saved_to") or ""
    folder = RUNS_DIR / (Path(saved_to).stem or "_без-черновика")
    folder.mkdir(parents=True, exist_ok=True)
    record = {k: job.get(k) for k in ("id", "title", "started", "finished", "steps", "result", "log")}
    stamp = time.strftime("%Y-%m-%d_%H-%M", time.localtime(job["started"]))
    (folder / f"{stamp}_{job['id']}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def load_runs(draft_name: str) -> list[dict]:
    """История прогонов для черновика, от первого к последнему."""
    folder = RUNS_DIR / Path(draft_name).stem
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
