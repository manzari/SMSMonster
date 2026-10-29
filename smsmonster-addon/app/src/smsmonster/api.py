"""HTTP API + static UI (Starlette)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import secrets

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .db import Database
from .events import EventBus
from .manager import ModemManager
from .modem import ModemError
from .webhooks import WEBHOOK_EVENTS, WebhookDispatcher

log = logging.getLogger(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
INGRESS_HOSTS = ("172.30.32.2",)
MAX_BODY_BYTES = 64 * 1024
NUMBER_RE = re.compile(r"\+?[0-9]{1,20}")
USSD_RE = re.compile(r"[0-9*#+]{1,32}")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'"


class AppContext:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, manager: ModemManager) -> None:
        self.settings, self.db, self.bus, self.manager = settings, db, bus, manager
        self.webhooks = WebhookDispatcher(db)


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ctx: AppContext = request.app.state.ctx
        token = ctx.settings.api_token
        request.state.authorized = not token or self._authorized(request, token, ctx.settings.trust_ingress)
        if not request.state.authorized and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = CSP
        return response

    @staticmethod
    def _authorized(request: Request, token: str, trust_ingress: bool) -> bool:
        # request.client is the real TCP peer (uvicorn runs without proxy_headers), so it cannot be spoofed.
        if trust_ingress and request.client and request.client.host in INGRESS_HOSTS and request.headers.get("x-ingress-path"):
            return True
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidate = auth[7:].strip()
        elif request.headers.get("x-api-key"):
            candidate = request.headers["x-api-key"]
        elif request.url.path == "/api/events":  # EventSource cannot set headers
            candidate = request.query_params.get("token", "")
        else:
            candidate = ""
        return bool(candidate) and secrets.compare_digest(candidate.encode("utf-8", "replace"), token.encode("utf-8"))


def _utf8_clean(obj) -> bool:
    """Lone surrogates (\\ud800 escapes) cannot be stored or sent."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(obj, dict):
        return all(_utf8_clean(k) and _utf8_clean(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_utf8_clean(v) for v in obj)
    return True


async def _json_body(request: Request) -> dict:
    # Requiring the JSON content type makes browser requests non-simple, which blocks cross-site POSTs (CSRF).
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            raise HTTPException(413, "body too large")
    except ValueError:
        raise HTTPException(400, "invalid Content-Length")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "body too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object expected")
    if not _utf8_clean(data):
        raise HTTPException(400, "invalid unicode in body")
    return data


def _number(raw: str) -> str:
    n = raw.strip().replace(" ", "").replace("-", "")
    if not NUMBER_RE.fullmatch(n):
        raise HTTPException(400, "invalid number (digits with optional leading +)")
    return n


def _timeout(val, lo: float = 1.0, hi: float = 60.0) -> float:
    try:
        t = float(val)
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid timeout")
    if not math.isfinite(t):
        raise HTTPException(400, "invalid timeout")
    return min(max(t, lo), hi)


def _webhook_url(raw) -> str:
    url = str(raw).strip()
    if not url.startswith(("http://", "https://")) or len(url) > 2048:
        raise HTTPException(400, "url must be http(s)")
    return url


def _require(body: dict, key: str, typ=str) -> object:
    val = body.get(key)
    if val is None or (typ is str and not str(val).strip()) or not isinstance(val, typ):
        raise HTTPException(400, f"'{key}' is required")
    return val


# ---------- routes ----------

async def health(request: Request) -> Response:
    ctx = _ctx(request)
    if not request.state.authorized:
        return JSONResponse({"status": "ok", "version": __version__, "auth": True})
    workers = ctx.manager.list()
    return JSONResponse({"status": "ok", "version": __version__, "modems": len(workers),
                         "ready": sum(1 for w in workers if w.ready), "auth": bool(ctx.settings.api_token)})


async def stats(request: Request) -> Response:
    ctx = _ctx(request)
    s = await asyncio.to_thread(ctx.db.message_stats)
    s["modems"] = [w.to_dict() for w in ctx.manager.list()]
    return JSONResponse(s)


async def list_modems(request: Request) -> Response:
    return JSONResponse([w.to_dict() for w in _ctx(request).manager.list()])


def _worker(request: Request):
    w = _ctx(request).manager.get(request.path_params["modem_id"])
    if not w:
        raise HTTPException(404, "modem not found")
    return w


async def get_modem(request: Request) -> Response:
    return JSONResponse(_worker(request).to_dict())


async def patch_modem(request: Request) -> Response:
    w = _worker(request)
    body = await _json_body(request)
    name = (body.get("name") or "").strip() or None
    number = (body.get("phone_number") or "").strip() or None
    if w.state.imei:
        await asyncio.to_thread(_ctx(request).db.update_modem_settings, w.state.imei, name, number)
    w.state.name, w.state.phone_number = name, number
    _ctx(request).bus.publish("modem.updated", w.to_dict())
    return JSONResponse(w.to_dict())


async def _run_job(w, fn, timeout: float, need_ready: bool = True):
    if (need_ready and not w.ready) or not w.port_open:
        raise HTTPException(409, f"modem not ready (status: {w.state.status})")
    fut = w.submit(fn)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
    except asyncio.TimeoutError:
        fut.cancel()
        raise HTTPException(504, "modem did not respond in time")
    except ModemError as e:
        raise HTTPException(502, str(e))


async def modem_at(request: Request) -> Response:
    w = _worker(request)
    body = await _json_body(request)
    cmd = str(_require(body, "command")).strip()
    if not cmd.upper().startswith("AT") or len(cmd) > 512 or CTRL_RE.search(cmd):
        raise HTTPException(400, "command must be a single AT command")
    timeout = _timeout(body.get("timeout", 10))
    lines = await _run_job(w, lambda m: m.job_at(cmd, timeout=timeout), timeout + 15, need_ready=False)
    return JSONResponse({"command": cmd, "response": lines})


async def modem_ussd(request: Request) -> Response:
    w = _worker(request)
    body = await _json_body(request)
    code = str(_require(body, "code")).strip()
    if not USSD_RE.fullmatch(code):
        raise HTTPException(400, "invalid USSD code")
    res = await _run_job(w, lambda m: m.job_ussd(code), 60)
    return JSONResponse(res)


async def modem_restart(request: Request) -> Response:
    w = _worker(request)
    if w.port_open:
        await _run_job(w, lambda m: m.job_reset(), 20, need_ready=False)
    else:
        w.request_reconnect()
    return JSONResponse({"ok": True})


async def list_messages(request: Request) -> Response:
    q = request.query_params
    try:
        limit = int(q.get("limit", 50))
        before = int(q["before"]) if q.get("before") else None
    except ValueError:
        raise HTTPException(400, "invalid pagination parameter")
    rows = await asyncio.to_thread(_ctx(request).db.list_messages, q.get("direction"), q.get("modem_id"), q.get("number"),
                                   q.get("status"), q.get("kind"), q.get("search"), before, limit)
    return JSONResponse(rows)


async def get_message(request: Request) -> Response:
    row = await asyncio.to_thread(_ctx(request).db.get_message, int(request.path_params["message_id"]))
    if not row:
        raise HTTPException(404, "message not found")
    return JSONResponse(row)


async def delete_message(request: Request) -> Response:
    ok = await asyncio.to_thread(_ctx(request).db.delete_message, int(request.path_params["message_id"]))
    if not ok:
        raise HTTPException(404, "message not found")
    return Response(status_code=204)


async def send_message(request: Request) -> Response:
    ctx = _ctx(request)
    body = await _json_body(request)
    to = _number(str(_require(body, "to") if "to" in body else _require(body, "number")))
    text = str(_require(body, "text"))
    if len(text) > 1530:
        raise HTTPException(400, "text too long (max 1530 characters)")
    modem_id = (body.get("modem_id") or "").strip() or None
    if modem_id:
        w = ctx.manager.get(modem_id)
        if not w:
            raise HTTPException(404, "modem not found")
        modem_id = w.state.id
    elif not ctx.manager.list():
        raise HTTPException(503, "no modem connected")
    msg = await asyncio.to_thread(ctx.db.insert_message, direction="out", modem_id=modem_id, number=to, text=text, status="queued")
    ctx.bus.publish("message.updated", msg)
    return JSONResponse(msg, status_code=202)


async def retry_message(request: Request) -> Response:
    ctx = _ctx(request)
    mid = int(request.path_params["message_id"])
    row = await asyncio.to_thread(ctx.db.get_message, mid)
    if not row or row["direction"] != "out":
        raise HTTPException(404, "outgoing message not found")
    msg = await asyncio.to_thread(ctx.db.update_message, mid, status="queued", attempts=0, error=None)
    ctx.bus.publish("message.updated", msg)
    return JSONResponse(msg)


async def list_webhooks(request: Request) -> Response:
    return JSONResponse({"webhooks": await asyncio.to_thread(_ctx(request).db.list_webhooks), "events": list(WEBHOOK_EVENTS)})


def _validate_events(events) -> list[str]:
    if not isinstance(events, list) or not events:
        raise HTTPException(400, "'events' must be a non-empty list")
    bad = [e for e in events if e != "*" and e not in WEBHOOK_EVENTS]
    if bad:
        raise HTTPException(400, f"unknown events: {bad}")
    return events


async def add_webhook(request: Request) -> Response:
    body = await _json_body(request)
    url = _webhook_url(_require(body, "url"))
    events = _validate_events(body.get("events", ["message.received"]))
    hook = await asyncio.to_thread(_ctx(request).db.add_webhook, url, events, body.get("secret") or None, bool(body.get("enabled", True)))
    return JSONResponse(hook, status_code=201)


async def patch_webhook(request: Request) -> Response:
    body = await _json_body(request)
    fields = {}
    if "url" in body:
        fields["url"] = _webhook_url(body["url"])
    if "events" in body:
        fields["events"] = _validate_events(body["events"])
    if "secret" in body:
        fields["secret"] = body["secret"] or None
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    hook = await asyncio.to_thread(_ctx(request).db.update_webhook, int(request.path_params["webhook_id"]), **fields)
    if not hook:
        raise HTTPException(404, "webhook not found")
    return JSONResponse(hook)


async def delete_webhook(request: Request) -> Response:
    ok = await asyncio.to_thread(_ctx(request).db.delete_webhook, int(request.path_params["webhook_id"]))
    if not ok:
        raise HTTPException(404, "webhook not found")
    return Response(status_code=204)


async def test_webhook(request: Request) -> Response:
    ctx = _ctx(request)
    hooks = [h for h in await asyncio.to_thread(ctx.db.list_webhooks) if h["id"] == int(request.path_params["webhook_id"])]
    if not hooks:
        raise HTTPException(404, "webhook not found")
    await ctx.webhooks._deliver(hooks[0], "test", {"message": "SMS Monster webhook test"})
    return JSONResponse({"ok": True})


async def events(request: Request) -> Response:
    ctx = _ctx(request)
    q = ctx.bus.subscribe()

    async def gen():
        try:
            yield {"event": "hello", "data": json.dumps({"version": __version__})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": event, "data": json.dumps(data, default=str)}
        finally:
            ctx.bus.unsubscribe(q)

    return EventSourceResponse(gen())


async def index(request: Request) -> Response:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def http_exception(request: Request, exc: HTTPException) -> Response:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def create_app(settings: Settings, db: Database, bus: EventBus, manager: ModemManager) -> Starlette:
    ctx = AppContext(settings, db, bus, manager)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        bus.attach(asyncio.get_running_loop())
        bus.add_listener(ctx.webhooks)
        manager.start()
        log.info("SMS Monster %s listening on %s:%s", __version__, settings.host, settings.port)
        if not settings.api_token:
            log.warning("SMSMONSTER_API_TOKEN is not set: anyone who can reach port %s can read and send SMS", settings.port)
        try:
            yield
        finally:
            manager.stop()
            await ctx.webhooks.close()
            db.close()

    routes = [
        Route("/api/health", health),
        Route("/api/stats", stats),
        Route("/api/events", events),
        Route("/api/modems", list_modems),
        Route("/api/modems/{modem_id}", get_modem),
        Route("/api/modems/{modem_id}", patch_modem, methods=["PATCH"]),
        Route("/api/modems/{modem_id}/at", modem_at, methods=["POST"]),
        Route("/api/modems/{modem_id}/ussd", modem_ussd, methods=["POST"]),
        Route("/api/modems/{modem_id}/restart", modem_restart, methods=["POST"]),
        Route("/api/messages", list_messages),
        Route("/api/messages", send_message, methods=["POST"]),
        Route("/api/send", send_message, methods=["POST"]),
        Route("/api/messages/{message_id:int}", get_message),
        Route("/api/messages/{message_id:int}", delete_message, methods=["DELETE"]),
        Route("/api/messages/{message_id:int}/retry", retry_message, methods=["POST"]),
        Route("/api/webhooks", list_webhooks),
        Route("/api/webhooks", add_webhook, methods=["POST"]),
        Route("/api/webhooks/{webhook_id:int}", patch_webhook, methods=["PATCH"]),
        Route("/api/webhooks/{webhook_id:int}", delete_webhook, methods=["DELETE"]),
        Route("/api/webhooks/{webhook_id:int}/test", test_webhook, methods=["POST"]),
        Route("/", index),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(AuthMiddleware)],
                    exception_handlers={HTTPException: http_exception})
    app.state.ctx = ctx
    return app
