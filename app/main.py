"""BA-Account Swarm -- FastAPI entry-point with Mission-Control dashboard."""

from __future__ import annotations

import asyncio
import json
import sqlite3 as _sqlite
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTMLResponse, PlainTextResponse
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# App bootstrap                                                               #
# --------------------------------------------------------------------------- #

app = FastAPI(title="BA-Account Swarm", version="0.1.0")

_UI_DIR = Path(__file__).resolve().parent / ".." / "ui"


# --------------------------------------------------------------------------- #
# Pydantic request models                                                     #
# --------------------------------------------------------------------------- #

class SwarmRunReq(BaseModel):
    query: str = Field(..., min_length=1)
    caller: str = "web"


class HitlActionReq(BaseModel):
    id: str
    action: str  # "approve" or "deny"
    answerer: str = "web"


class ContextSaveReq(BaseModel):
    caller: str = "web"
    key: str
    value: Any


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


# --------------------------------------------------------------------------- #
# Lifecycle                                                                   #
# --------------------------------------------------------------------------- #

@app.on_event("startup")
def _bootstrap():
    """Initialise the local SQLite database at startup."""
    from .db import init_db, get_db_path
    init_db()
    path = get_db_path()
    print(f"[ba-account-swarm] DB file: {path}")


# --------------------------------------------------------------------------- #
# HTTP routes                                                                 #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    p = _UI_DIR / "index.html"
    if p.exists():
        content = p.read_text(encoding="utf-8")
        return HTMLResponse(content, media_type="text/html")
    return HTMLResponse("<h1>BA Swarm - Mission Control</h1>", media_type="text/html")


@app.get("/ui/{filename:path}")
async def serve_ui(filename: str):
    """Serve static UI assets (CSS, JS, etc)."""
    p = _UI_DIR / filename
    if p.exists() and p.is_file():
        ext = p.suffix.lower()
        media_types = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        ct = media_types.get(ext, "application/octet-stream")
        return FileResponse(p, media_type=ct)
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.get("/pwa-manifest.json", media_type="application/json")
def pwa_manifest():
    m = {
        "name": "BA Account Swarm",
        "short_name": "Swarm",
        "start_url": "/",
        "display": "standalone",
        "icons": [{"src": "/ui/icon-192.png", "sizes": "192x192", "type": "image/png"}],
    }
    return PlainTextResponse(json.dumps(m, indent=2), media_type="application/json")


@app.post("/swarm/run", response_class=JSONResponse)
async def swarm_run(req: SwarmRunReq):
    """Kick off a swarm session and wait for completion."""
    from ..swarm.engine import run_swarm as _rs

    result = await _rs(user_query=req.query, caller=req.caller)
    return JSONResponse(result)


@app.get("/hitl/pending")
async def hitl_pending(caller: str = "web"):
    """Return pending Human-in-the-Loop requests for a given caller."""
    from ..app.db import hitl_get_pending

    pending = hitl_get_pending(caller)
    return JSONResponse({
        "pending": [
            {
                "id": p.id,
                "question": p.description,
                "tool_name": p.tool_name,
                "created_at": p.requested_at,
            }
            for p in pending
        ]
    })


@app.post("/hitl/action", response_class=JSONResponse)
async def hitl_action(req: HitlActionReq):
    """Record a HITL approve/deny action."""
    from ..app.db import get_db_path, hitl_update

    status = "approved" if req.action == "approve" else "denied"

    # Use the domain API (handles WAL mode, retries internally)
    ok = hitl_update(req.id, status, answerer=req.answerer)
    if ok:
        return JSONResponse({"ok": True, "id": req.id, "status": status})
    return JSONResponse({"ok": False, "error": "ID not found"}, status_code=404)


@app.post("/context/save", response_class=JSONResponse)
async def context_save(req: ContextSaveReq):
    """Persist key-value context for a caller."""
    from ..app.db import save_context

    try:
        save_context(req.caller, req.key, req.value)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


@app.get("/context/get")
def context_get(caller: str = "web", key: str = ""):
    """Retrieve stored context for a caller and key."""
    from ..app.db import get_context

    value = get_context(caller, key)
    return JSONResponse({"key": key, "value": value})


@app.get("/audit/{session_id}")
async def audit_log(session_id: str):
    """Return the full swarm execution trace for a session."""
    from ..app.db import get_audit_logs

    events = get_audit_logs(session_id, limit=100)
    return JSONResponse({"session_id": session_id, "events": events})


@app.get("/health")
def health_check():
    """Minimal liveness probe."""
    from ..app.db import get_db_path

    db = get_db_path()
    status = "sqlite-wal" if db.exists() else "no-db"
    return JSONResponse({"status": "ok", "db": status, "db_path": str(db)})


# --------------------------------------------------------------------------- #
# WebSocket /chat  (Mission Control live chat)                                #
# --------------------------------------------------------------------------- #

@app.websocket("/chat")
async def websocket_chat(websocket: WebSocket):
    """Live chat endpoint for the Mission Control dashboard.

    Protocol (JSON):
      Incoming: {"type": "query", "content": "..."} | {"type": "hitl_action", ...}
      Outgoing: {"type": "status", ...} | {"type": "result", ...} | {"type": "event", ...}
    """
    await websocket.accept()

    try:
        await websocket.send_json({"type": "status", "text": "Connected -- send a query."})

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json({"type": "error", "text": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")

            # --- HITL quick-action --------------------------------------------------
            if msg_type == "hitl_action":
                from ..app.db import get_db_path, hitl_update as _hud
                st = "approved" if msg.get("action") == "approve" else "denied"
                ok = _hud(msg.get("id", ""), st, answerer=msg.get("answerer", "ws"))
                if ok:
                    await websocket.send_json({"type": "status", "text": f"HITL {st}"})
                else:
                    await websocket.send_json({"type": "error", "text": "HITL not found"})
                continue

            # --- Swarm query --------------------------------------------------------
            if msg_type == "query":
                user_query = msg.get("content", "").strip()
                if not user_query:
                    await websocket.send_json({"type": "error", "text": "Empty query"})
                    continue

                caller = msg.get("caller", "web")
                await websocket.send_json({"type": "status", "text": "Starting swarm..."})

                from ..swarm.engine import run_swarm as _rs

                task = asyncio.create_task(_rs(user_query, caller))

                try:
                    result = await asyncio.wait_for(task, timeout=300)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "text": "Swarm timed out (180s)"})
                    continue
                except Exception as exc:
                    await websocket.send_json({"type": "error", "text": f"Swarm error: {exc}"})
                    continue

                session_id = result.get("session_id", "")
                result_text = result.get("result", "")

                from ..app.db import get_audit_logs
                events = get_audit_logs(session_id, limit=50)

                await websocket.send_json({
                    "type": "result",
                    "session_id": session_id,
                    "result": result_text,
                    "agents_run": result.get("agents_run", []),
                    "wall_seconds": result.get("wall_seconds", 0),
                })
                for ev in events:
                    await websocket.send_json({"type": "event", **ev})

                await websocket.send_json({"type": "status", "text": "Done."})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "text": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
