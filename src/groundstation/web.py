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


@app.get("/api/revgeo")
def api_revgeo(lat: float, lon: float):
    return tools.reverse_geocode(lat, lon)


@app.get("/api/scenes")
def api_scenes(w: float, s: float, e: float, n: float, days: int = 14, max_cloud: float = 40):
    r = tools.search_imagery(
        "earth-search",
        ["sentinel-2-l2a"],
        bbox=[w, s, e, n],
        datetime_range=tools.last_days_window(days),
        max_cloud_cover=max_cloud,
        limit=12,
    )
    for it in r.get("items", []):
        it["tiles"] = tools.tile_url_template("earth-search", it["collection"], it["id"], ["visual"])
    return r


@app.get("/api/events")
def api_events(w: float, s: float, e: float, n: float, days: int = 30):
    return tools.active_events(bbox=[w, s, e, n], days=days, pad=0.4)


@app.get("/api/weather")
def api_weather(lat: float, lon: float):
    return tools.weather_summary(lat, lon)


@app.get("/api/ndvi")
def api_ndvi(collection: str, item: str):
    return tools.compute_statistics("earth-search", collection, item, expression="(nir-red)/(nir+red)")


@app.get("/api/compare")
def api_compare(place: str, w: float, s: float, e: float, n: float):
    return tools.compare_dates(place=place, bbox=[w, s, e, n])


@app.get("/api/artifacts")
def api_artifacts():
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


class InsightReq(BaseModel):
    query: str
    data: dict


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
            # insight/ask output is one long paragraph line — don't truncate it
            job["log"].append(line.rstrip()[:4000])
        proc.wait(timeout=600)
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:  # job must always resolve, UI polls it
        job["log"].append(str(e))
        job["status"] = "error"


def _start_job(argv: list[str], input_text: str | None = None) -> str:
    # keep memory bounded: drop the oldest finished jobs beyond the newest 20
    done = [k for k, j in JOBS.items() if j["status"] != "running"]
    for k in done[:-20] if len(done) > 20 else []:
        JOBS.pop(k, None)
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
        "For vegetation/water index layers on maps, pass expression (e.g. '(nir-red)/(nir+red)') "
        "with rescale='-1,1' and colormap_name='rdylgn' on the render_map item layer — never bare "
        "nir/red assets, that renders blank. If the answer is spatial, finish with render_map and "
        "state the map file path on its own final line as MAP: <path>. Be concise, use markdown.\n\n"
        "Question: " + req.question
    )
    return {"job": _start_job(argv, input_text=prompt)}


INSIGHT_PROMPT = """You are the interpretation layer of an Earth data console. The user scanned a
place; their exact query was: {query!r}. Below is everything the console gathered (fresh scenes,
active events, weather, NDVI vegetation stats).

Write 3-5 plain sentences, one paragraph, no headers or bullets: what the user is looking at, what
is notable in this data, and what it does or does not say about what their query implies they care
about. Cite dates and numbers only from the data. Be honest about limits — if their implied
question needs comparison over time, higher-resolution imagery, or analysis this scan didn't do,
say so in one clause and point at the right next step ("Ask the agent" for comparisons and custom
analysis, "Generate Earth brief" for a monitoring report). End with one concrete thing to try next.

DATA:
"""


@app.post("/api/insight")
def api_insight(req: InsightReq):
    prompt = INSIGHT_PROMPT.format(query=req.query) + json.dumps(req.data, default=str)
    argv = ["claude", "-p", "--model", "sonnet"]
    return {"job": _start_job(argv, input_text=prompt)}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {"status": job["status"], "log": job["log"][-30:]}


DEMO.mkdir(exist_ok=True)
app.mount("/demo", StaticFiles(directory=DEMO), name="demo")
app.mount("/docs", StaticFiles(directory=REPO / "docs"), name="docs")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
