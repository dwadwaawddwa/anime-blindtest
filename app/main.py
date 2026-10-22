"""blindtest.dwa.gold — blind test sur les musiques des listes anime, seul ou à plusieurs.

Connexion par SSO depuis anime.dwa.gold. Les parties se jouent en temps réel par WebSocket
(`/ws/{code}`) ; les extraits sont servis par ce serveur sous des jetons aléatoires.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from starlette.middleware.sessions import SessionMiddleware

from . import catalog as cat
from . import clips, config, game
from .db import Game, GamePlayer, GameRound, Preset, db_session, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("blindtest")

ASSET_V = str(int(time.time()))
_stop_thread = threading.Event()
_stop_async: asyncio.Event | None = None


def _wanted_songs():
    """Versions TV à précharger en priorité : celles des listes des membres, des plus
    connues aux plus pointues (ce sont elles qui sortent le plus en partie)."""
    now = time.time()
    if now - _wanted_cache["at"] > 600:
        try:
            with cat._ro(config.ANIME_DB) as c:
                uids = [r[0] for r in c.execute("select distinct user_id from entries")]
            roots = set()
            for uid in uids:
                roots |= cat.user_roots(uid, list(game.STATUSES))
            songs = [s for r in roots for s in cat.catalog.by_root.get(r, ())
                     if s.kind in ("OP", "ED", "IN") and s.at_audio]
            _wanted_cache.update(at=now, songs=sorted(songs, key=lambda s: s.difficulty))
        except Exception:
            log.exception("liste de préchargement")
    return _wanted_cache["songs"]


_wanted_cache = {"at": 0.0, "songs": []}


@asynccontextmanager
async def lifespan(_app):
    global _stop_async
    init_db()
    cat.catalog.load()
    threading.Thread(target=cat.refresher, args=(_stop_thread,), daemon=True).start()
    _stop_async = asyncio.Event()
    tasks = [asyncio.create_task(game.janitor(_stop_async))]
    if config.PRECACHE:
        tasks.append(asyncio.create_task(clips.precache_loop(_wanted_songs, _stop_async)))
    yield
    _stop_thread.set()
    _stop_async.set()
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="bt_session",
                   https_only=config.COOKIE_SECURE, same_site="lax", max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=os.path.join(config.BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(config.BASE_DIR, "templates"))

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: https://media.kitsu.app https://media.kitsu.io https://s4.anilist.co "
    "https://cdn.discordapp.com " + config.ANIME_SITE_URL,
    "media-src 'self' blob: data:",
    "connect-src 'self' " + config.BASE_URL.replace("https://", "wss://").replace("http://", "ws://"),
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith(("/api/", "/clip/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- session / SSO
def current_uid(request) -> str | None:
    uid = request.session.get("uid")
    if uid:
        return uid
    if config.LOCAL_USER_ID:
        request.session["uid"] = config.LOCAL_USER_ID
        return config.LOCAL_USER_ID
    return None


def require_uid(request: Request) -> str:
    uid = current_uid(request)
    if not uid:
        raise HTTPException(401, "Connexion requise")
    return uid


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_hex(16)
    return tok


def check_csrf(request: Request):
    good, got = request.session.get("csrf"), request.headers.get("x-csrf", "")
    if not good or not got or not secrets.compare_digest(good, got):
        raise HTTPException(403, "Jeton CSRF invalide, recharge la page")


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON invalide")
    return data


def page(request: Request, name: str, **ctx):
    uid = current_uid(request)
    user = cat.user_info(uid) if uid else None
    return templates.TemplateResponse(request, name, {
        "v": ASSET_V, "user": user, "csrf": csrf_token(request), "anime_url": config.ANIME_SITE_URL,
        "music_url": config.MUSIC_SITE_URL, **ctx})


@app.get("/login")
def login(request: Request, next: str = "/"):
    if current_uid(request):
        return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", 303)
    if not config.SSO_SECRET:
        return HTMLResponse("<h1>SSO non configuré</h1>", status_code=503)
    state = secrets.token_urlsafe(24)
    request.session["sso_state"] = state
    if next.startswith("/room/"):  # rejoindre un salon après la connexion
        request.session["after_login"] = next[:20]
    return RedirectResponse(f"{config.ANIME_SITE_URL}/sso/blindtest?state={state}", status_code=303)


@app.get("/auth/sso")
def auth_sso(request: Request, uid: str = "", ts: str = "", state: str = "", sig: str = ""):
    expected = request.session.pop("sso_state", None)
    after = request.session.pop("after_login", "/")
    if not (config.SSO_SECRET and uid and ts.isdigit() and state and sig and expected):
        raise HTTPException(400, "Connexion invalide, réessaie")
    if not secrets.compare_digest(state, expected):
        raise HTTPException(400, "Connexion invalide (état)")
    if abs(time.time() - int(ts)) > 300:
        raise HTTPException(400, "Lien de connexion expiré, réessaie")
    msg = f"blindtest|{uid}|{ts}|{state}".encode()
    if not hmac.compare_digest(hmac.new(config.SSO_SECRET.encode(), msg, hashlib.sha256).hexdigest(), sig):
        raise HTTPException(403, "Signature invalide")
    if cat.user_info(uid) is None:
        raise HTTPException(403, "Compte inconnu")
    request.session.clear()
    request.session["uid"] = uid
    request.session["csrf"] = secrets.token_hex(16)
    return RedirectResponse(after if after.startswith("/room/") else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    uid = current_uid(request)
    recent = []
    if uid:
        s = db_session()
        try:
            rows = (s.query(Game, GamePlayer).join(GamePlayer, GamePlayer.game_id == Game.id)
                    .filter(GamePlayer.user_id == uid).order_by(Game.id.desc()).limit(6).all())
            recent = [{"id": g.id, "rank": gp.rank, "players": g.players, "score": gp.score,
                       "rounds": g.rounds, "correct": gp.correct, "date": g.ended_at} for g, gp in rows]
        finally:
            s.close()
    return page(request, "home.html", recent=recent, top=leaderboard(5))


@app.get("/room/{code}", response_class=HTMLResponse)
def room_page(request: Request, code: str):
    code = code.upper()
    if not current_uid(request):
        return RedirectResponse(f"/login?next=/room/{code}", status_code=303)
    if code not in game.rooms:
        return page(request, "notfound.html", code=code)
    return page(request, "room.html", code=code, presets=game.PRESETS, defaults=game.DEFAULTS)


def leaderboard(limit: int = 100) -> list[dict]:
    s = db_session()
    try:
        rows = (s.query(GamePlayer.user_id, func.max(GamePlayer.name), func.count(GamePlayer.id),
                        func.sum(GamePlayer.score), func.sum(GamePlayer.correct), func.sum(GamePlayer.answered),
                        func.max(GamePlayer.score), func.max(GamePlayer.best_streak))
                .group_by(GamePlayer.user_id).all())
        wins = dict(s.query(GamePlayer.user_id, func.count(GamePlayer.id))
                    .join(Game, Game.id == GamePlayer.game_id)
                    .filter(GamePlayer.rank == 1, Game.players >= 2).group_by(GamePlayer.user_id).all())
    finally:
        s.close()
    out = []
    for uid, name, games, total, correct, answered, best, streak in rows:
        info = cat.user_info(uid) or {"name": name, "avatar": ""}
        out.append({"id": uid, "name": info["name"], "avatar": info["avatar"], "games": games,
                    "total": total or 0, "wins": wins.get(uid, 0), "best": best or 0, "streak": streak or 0,
                    "accuracy": round(100 * (correct or 0) / answered) if answered else 0})
    out.sort(key=lambda r: (-r["total"], -r["wins"]))
    return out[:limit]


@app.get("/classement", response_class=HTMLResponse)
def ranking_page(request: Request):
    return page(request, "ranking.html", rows=leaderboard())


@app.get("/partie/{gid}", response_class=HTMLResponse)
def game_page(request: Request, gid: int):
    s = db_session()
    try:
        g = s.get(Game, gid)
        if g is None:
            raise HTTPException(404, "Partie introuvable")
        players = s.query(GamePlayer).filter_by(game_id=gid).order_by(GamePlayer.rank).all()
        rounds = s.query(GameRound).filter_by(game_id=gid).order_by(GameRound.idx).all()
    finally:
        s.close()
    infos = {p.user_id: cat.user_info(p.user_id) for p in players}
    return page(request, "game.html", g=g, players=players, rounds=rounds, infos=infos,
                settings=json.loads(g.settings or "{}"))


# --------------------------------------------------------------------------- API
@app.post("/api/rooms")
async def create_room(request: Request):
    uid = require_uid(request)
    check_csrf(request)
    data = await body(request)
    room = game.new_room(uid)
    preset = data.get("preset")
    if preset in game.PRESETS:
        room.settings, room.preset = game.preset_settings(preset), preset
    return {"code": room.code}


@app.get("/api/rooms/{code}")
def room_exists(request: Request, code: str):
    require_uid(request)
    return {"exists": code.upper() in game.rooms}


@app.get("/api/search")
def search(request: Request, q: str = ""):
    require_uid(request)
    return {"results": [game.anime_card(a) for a in cat.catalog.search(q[:80])]}


@app.get("/api/presets")
def list_presets(request: Request):
    uid = require_uid(request)
    s = db_session()
    try:
        mine = s.query(Preset).filter_by(user_id=uid).order_by(Preset.id).all()
        return {"builtin": game.PRESETS, "mine": [{"id": p.id, "name": p.name, "settings": json.loads(p.settings)}
                                                  for p in mine]}
    finally:
        s.close()


@app.post("/api/presets")
async def save_preset(request: Request):
    uid = require_uid(request)
    check_csrf(request)
    data = await body(request)
    name = str(data.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Donne un nom au preset")
    settings = game.clean_settings(data.get("settings") or {})
    s = db_session()
    try:
        if s.query(Preset).filter_by(user_id=uid).count() >= 30:
            raise HTTPException(400, "30 presets maximum")
        s.add(Preset(user_id=uid, name=name, settings=json.dumps(settings)))
        s.commit()
    finally:
        s.close()
    return list_presets(request)


@app.post("/api/presets/{pid}/delete")
def delete_preset(request: Request, pid: int):
    uid = require_uid(request)
    check_csrf(request)
    s = db_session()
    try:
        p = s.get(Preset, pid)
        if p is None or p.user_id != uid:
            raise HTTPException(404, "Preset introuvable")
        s.delete(p)
        s.commit()
    finally:
        s.close()
    return list_presets(request)


@app.get("/clip/{token}.mp3")
def clip(token: str):
    path = clips.round_file(token)
    if not path:
        raise HTTPException(404, "Extrait expiré")
    return FileResponse(path, media_type="audio/mpeg")


# --------------------------------------------------------------------------- temps réel
def _origin_ok(ws: WebSocket) -> bool:
    """Le cookie de session part avec la connexion : on refuse les autres sites."""
    origin = ws.headers.get("origin", "")
    allowed = {config.BASE_URL}
    if "127.0.0.1" in config.BASE_URL:
        allowed.add(config.BASE_URL.replace("127.0.0.1", "localhost"))
    return origin in allowed


@app.websocket("/ws/{code}")
async def ws_room(ws: WebSocket, code: str):
    uid = ws.session.get("uid") or config.LOCAL_USER_ID or None
    room = game.rooms.get(code.upper())
    if not _origin_ok(ws) or not uid or room is None:
        await ws.close(code=4401 if not uid else 4404)
        return
    user = cat.user_info(uid)
    if user is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    if not await room.join(ws, user):
        await ws.close()
        return
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 20000:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "ping":
                await ws.send_text('{"t":"pong"}')
            elif t == "loaded" and msg.get("idx") == room.idx:
                room.loaded.add(uid)
                room.wake.set()
            elif t == "answer":
                if room.answer(uid, int(msg.get("idx", -1)), int(msg.get("choice") or 0)):
                    await room.broadcast({"t": "answered", "id": uid})
            elif t == "settings":
                await room.update_settings(uid, msg.get("settings") or {}, msg.get("preset"))
            elif t == "start":
                await room.start(uid)
            elif t == "stop":
                await room.stop(uid)
            elif t == "next":
                await room.next(uid)
            elif t == "lobby":
                await room.back_to_lobby(uid)
            elif t == "kick":
                await room.kick(uid, str(msg.get("id") or ""))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket %s", code)
    finally:
        await room.leave(ws, uid)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Erreur</title>"
                        f"<link rel=stylesheet href='/static/app.css?v={ASSET_V}'><body class=center-page>"
                        f"<main class=card><h1>{exc.status_code}</h1><p>{html.escape(str(exc.detail))}</p>"
                        f"<a class='btn accent' href='/'>Retour</a></main>", status_code=exc.status_code)
