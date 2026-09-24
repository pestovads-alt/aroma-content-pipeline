"""
Аромапост — локальная веб-страница для контент-конвейера.

Запуск:  python app.py  →  http://127.0.0.1:5050
"""

import html
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import pipeline
from telegram_publish import join_frontmatter, publish_file, split_frontmatter

ROOT = Path(__file__).parent
POSTS_DIR = ROOT / "posts"

app = Flask(__name__)

STATUS_LABELS = {
    "draft": "Черновик",
    "published": "Опубликован",
    "rejected": "Отклонён",
}


@app.before_request
def same_origin_only():
    """Не даёт чужим сайтам в том же браузере нажать «Опубликовать» за вас."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            abort(403)


def telegram_to_html(text: str) -> str:
    """Превью Telegram-разметки: *жирный*, **жирный**, _курсив_, переносы строк."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"\*(.+?)\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w/])_(.+?)_(?![\w/])", r"<i>\1</i>", out, flags=re.DOTALL)
    return out.replace("\n", "<br>")


def draft_path(name: str) -> Path:
    path = (POSTS_DIR / name).resolve()
    if path.parent != POSTS_DIR.resolve() or path.suffix != ".md" or not path.exists():
        abort(404)
    return path


def load_draft(path: Path) -> dict:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    status = meta.get("status", "draft")
    return {
        "name": path.name,
        "meta": meta,
        "body": body,
        "html": telegram_to_html(body),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "length": len(body),
    }


@app.template_filter("clock")
def clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else ""


@app.template_filter("minutes")
def minutes(run: dict) -> str:
    if not (run.get("started") and run.get("finished")):
        return ""
    return f"{max(1, round((run['finished'] - run['started']) / 60))} мин"


@app.get("/")
def index():
    drafts = [load_draft(p) for p in sorted(POSTS_DIR.glob("*.md"), reverse=True)]
    return render_template("index.html", drafts=drafts, running=pipeline.active_job())


@app.post("/run")
def run():
    source = request.form.get("source", "").strip()
    focus = request.form.get("focus", "").strip()
    if not source:
        return redirect(url_for("index"))
    if job := pipeline.active_job():
        return redirect(url_for("job_page", job_id=job["id"]))
    title = source if len(source) <= 80 else source[:77] + "…"
    job_id = pipeline.start_job(pipeline.build_prompt(source, focus), title)
    return redirect(url_for("job_page", job_id=job_id))


@app.get("/job/<job_id>")
def job_page(job_id):
    job = pipeline.jobs.get(job_id) or abort(404)
    return render_template("job.html", job=job, steps=pipeline.STEPS)


@app.get("/api/job/<job_id>")
def job_api(job_id):
    job = pipeline.jobs.get(job_id) or abort(404)
    return jsonify({k: job.get(k) for k in ("state", "steps", "current", "result", "error", "log", "started")})


@app.get("/draft/<name>")
def draft_page(name):
    return render_template("draft.html", draft=load_draft(draft_path(name)),
                           runs=pipeline.load_runs(name), steps=pipeline.STEPS,
                           running=pipeline.active_job())


@app.post("/draft/<name>/publish")
def draft_publish(name):
    result = publish_file(str(draft_path(name)))
    return jsonify(result), (200 if result["status"] == "ok" else 400)


@app.post("/draft/<name>/revise")
def draft_revise(name):
    feedback = request.form.get("feedback", "").strip()
    path = draft_path(name)
    if not feedback:
        return redirect(url_for("draft_page", name=name))
    if job := pipeline.active_job():
        return redirect(url_for("job_page", job_id=job["id"]))
    prompt = pipeline.build_revision_prompt(str(path.relative_to(ROOT)), feedback)
    # факты уже проверены в первом прогоне — заново идут только писатель и финальная проверка
    job_id = pipeline.start_job(prompt, f"Доработка: {name}",
                                skip=("aroma-research-analyzer", "aroma-fact-checker"))
    return redirect(url_for("job_page", job_id=job_id))


@app.post("/draft/<name>/reject")
def draft_reject(name):
    path = draft_path(name)
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    if meta.get("status") != "published":
        meta["status"] = "rejected"
        path.write_text(join_frontmatter(meta, body), encoding="utf-8")
    return redirect(url_for("draft_page", name=name))


if __name__ == "__main__":
    POSTS_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5050, debug=False)
