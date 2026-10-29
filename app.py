"""Mini AI Business Intelligence Assistant - Flask backend.

Run:  python app.py        then open http://127.0.0.1:5000

Endpoints
  GET  /                  simple web UI
  POST /api/ask           {"question": "..."}  -> answer + supporting data
  GET  /api/data-quality  rows rejected while loading the CSV
  GET  /api/health        liveness + load summary
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from bi.analytics import run_plan
from bi.database import build_database, get_catalog, get_data_quality, get_readonly_connection
from bi.errors import QuestionError
from bi.interpreter import interpret
from bi.llm import make_llm_func, settings_from_env

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE per line) so no extra package is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def create_app(config: dict | None = None) -> Flask:
    cfg = {
        "CSV_PATH": BASE_DIR / "data" / "sales.csv",
        "DB_PATH": BASE_DIR / "data" / "sales.db",
    }
    cfg.update(config or {})

    app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False

    # 1) Data management: CSV -> cleaned rows -> SQLite
    summary = build_database(cfg["CSV_PATH"], cfg["DB_PATH"])
    with get_readonly_connection(cfg["DB_PATH"]) as conn:
        catalog = get_catalog(conn)

    # 2) AI integration (optional). Tests can pass LLM_FUNC explicitly (even None).
    llm_func = cfg["LLM_FUNC"] if "LLM_FUNC" in cfg else make_llm_func()
    app.logger.info("Loaded %s; LLM enabled: %s", summary, llm_func is not None)

    def error_response(message: str, code: str, status: int, suggestions=None):
        body = {"status": "error", "error": {"code": code, "message": message}}
        if suggestions:
            body["error"]["suggestions"] = suggestions
        return jsonify(body), status

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "data_load": summary, "llm_enabled": llm_func is not None})

    @app.get("/api/data-quality")
    def data_quality():
        with get_readonly_connection(cfg["DB_PATH"]) as conn:
            return jsonify({"status": "ok", "data_load": summary, **get_data_quality(conn)})

    @app.post("/api/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "question" not in payload:
            return error_response('Send JSON like {"question": "What is the total revenue?"}.',
                                  "invalid_request", 400)
        question = payload["question"]
        conn = get_readonly_connection(cfg["DB_PATH"])
        try:
            interpretation = interpret(question, catalog, llm_func)
            result = run_plan(conn, interpretation["plan"])
        except QuestionError as exc:
            return error_response(exc.message, exc.code, exc.status, exc.suggestions)
        except Exception:  # never leak internals to the client
            app.logger.exception("Unexpected error while answering %r", question)
            return error_response("Something went wrong on our side. Please try again.", "internal_error", 500)
        finally:
            conn.close()

        if summary["rejected"]:
            result["assumptions"].append(
                f"{summary['rejected']} invalid CSV row(s) were excluded at load time (see /api/data-quality).")
        return jsonify({
            "status": "ok",
            "question": " ".join(question.split()),
            "answer": result["answer"],
            "interpretation": {
                "intent": interpretation["plan"]["intent"],
                "dimension": interpretation["plan"]["dimension"],
                "order": interpretation["plan"]["order"] if interpretation["plan"]["dimension"] else None,
                "months": interpretation["plan"]["months"],
                "filters": interpretation["plan"]["filters"],
                "interpreted_by": interpretation["interpreted_by"],
                "notes": interpretation["notes"],
            },
            "supporting_data": result["supporting_data"],
            "sql": result["sql"],
            "sql_params": result["sql_params"],
            "assumptions": result["assumptions"],
        })

    @app.errorhandler(404)
    def not_found(_):
        return error_response("Not found.", "not_found", 404)

    @app.errorhandler(405)
    def method_not_allowed(_):
        return error_response("Method not allowed.", "method_not_allowed", 405)

    @app.errorhandler(500)
    def server_error(_):
        return error_response("Something went wrong on our side.", "internal_error", 500)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _load_env_file(BASE_DIR / ".env")
    settings = settings_from_env()
    print(f"LLM provider: {settings['provider']} (model: {settings['model'] or '-'})")
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
