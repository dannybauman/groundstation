"""groundstation web console: the no-CLI, no-chat-client door into the tools.

    uv run --group web groundstation-web
    # then open http://127.0.0.1:8765

Wraps the same tool functions the MCP server exposes, plus job endpoints for
the two long operations (Earth brief generation, free-text agent ask).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from groundstation import tools

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "demo"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="groundstation console")
JOBS: dict[str, dict] = {}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/geocode")
def api_geocode(q: str):
    return tools.geocode(q)


@app.get("/api/scenes")
def api_scenes(w: float, s: float, e: float, n: float, days: int = 14, max_cloud: float = 40):
    import datetime as dt

    today = dt.date.today()
    since = today - dt.timedelta(days=days)
    r = tools.search_imagery(
        "earth-search",
        ["sentinel-2-l2a"],
        bbox=[w, s, e, n],
        datetime_range=f"{since.isoformat()}T00:00:00Z/{today.isoformat()}T23:59:59Z",
        max_cloud_cover=max_cloud,
        limit=12,
    )
    for it in r.get("items", []):
        it["tiles"] = tools.tile_url_template("earth-search", it["collection"], it["id"], ["visual"])
    return r


@app.get("/api/events")
def api_events(w: float, s: float, e: float, n: float, days: int = 30):
    pad = 0.4
    return tools.active_events(bbox=[w - pad, s - pad, e + pad, n + pad], days=days)


@app.get("/api/weather")
def api_weather(lat: float, lon: float):
    return tools.weather_summary(lat, lon)


@app.get("/api/ndvi")
def api_ndvi(collection: str, item: str):
    return tools.compute_statistics("earth-search", collection, item, expression="(nir-red)/(nir+red)")


@app.get("/api/artifacts")
def api_artifacts():
    DEMO.mkdir(exist_ok=True)
    files = sorted(DEMO.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"name": p.name, "mtime": int(p.stat().st_mtime), "kind": "brief" if p.name.startswith("brief-") and "map" not in p.name else ("sweep" if p.name.startswith("morning") else "map")}
        for p in files[:40]
    ]


class BriefReq(BaseModel):
    place: str
    days: int = 7


class AskReq(BaseModel):
    question: str


def _run_job(job_id: str, argv: list[str], input_text: str | None = None) -> None:
    job = JOBS[job_id]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
            shell=(sys.platform == "win32"),
        )
        if input_text:
            proc.stdin.write(input_text)
            proc.stdin.close()
        for line in proc.stdout:
            job["log"].append(line.rstrip()[:400])
        proc.wait(timeout=600)
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:  # job must always resolve, UI polls it
        job["log"].append(str(e))
        job["status"] = "error"


def _start_job(argv: list[str], input_text: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "log": []}
    threading.Thread(target=_run_job, args=(job_id, argv, input_text), daemon=True).start()
    return job_id


@app.post("/api/brief")
def api_brief(req: BriefReq):
    argv = [sys.executable, str(REPO / "briefing" / "brief.py"), "--place", req.place, "--days", str(req.days)]
    return {"job": _start_job(argv)}


@app.post("/api/ask")
def api_ask(req: AskReq):
    mcp_config = json.dumps(
        {"mcpServers": {"groundstation": {"command": "uv", "args": ["--directory", str(REPO), "run", "groundstation"]}}}
    )
    argv = [
        "claude", "-p",
        "--mcp-config", mcp_config,
        "--strict-mcp-config",
        "--allowedTools", "mcp__groundstation",
        "--model", "sonnet",
    ]
    prompt = (
        "You have groundstation MCP tools (Earth observation: geocode, STAC search, previews, "
        "statistics, render_map, events, weather). Answer the user's question with them. "
        "If the answer is spatial, finish with render_map and state the map file path on its own "
        "final line as MAP: <path>. Be concise.\n\nQuestion: " + req.question
    )
    return {"job": _start_job(argv, input_text=prompt)}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {"status": job["status"], "log": job["log"][-30:]}


DEMO.mkdir(exist_ok=True)
app.mount("/demo", StaticFiles(directory=DEMO), name="demo")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
