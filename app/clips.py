"""Extraits audio des manches.

On ne donne JAMAIS l'URL d'origine au navigateur : le nom de fichier AnimeThemes
(« ShingekiNoKyojin-OP1.ogg ») ou l'id Deezer donneraient la réponse. Chaque manche
reçoit un petit mp3 découpé et nettoyé (sans métadonnées), servi sous un jeton aléatoire.

Sources, mises en cache sur disque :
- AnimeThemes : la version TV (celle de l'anime), mais serveur souvent lent → préchargée
  en tâche de fond ;
- Deezer : extrait de 30 s de la version complète, CDN rapide → récupéré à la volée.
"""
import asyncio
import logging
import os
import secrets
import time

import httpx

from . import config

log = logging.getLogger("blindtest.clips")

AT_DIR = os.path.join(config.CACHE_DIR, "at")
DZ_DIR = os.path.join(config.CACHE_DIR, "dz")
ROUND_DIR = os.path.join(config.CACHE_DIR, "rounds")
for d in (AT_DIR, DZ_DIR, ROUND_DIR):
    os.makedirs(d, exist_ok=True)

_failed: dict[str, float] = {}       # source en échec → moment (on ne réessaie pas avant 1 h)
_locks: dict[str, asyncio.Lock] = {}


class ClipError(Exception):
    pass


def at_path(song) -> str:
    return os.path.join(AT_DIR, f"{song.id}.mp3")


def dz_path(song) -> str:
    return os.path.join(DZ_DIR, f"{song.id}.mp3")


def has_tv(song) -> bool:
    return os.path.exists(at_path(song))


async def _run(*args, timeout: float = 60) -> bytes:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ClipError(f"{os.path.basename(args[0])} trop long")
    if proc.returncode != 0:
        raise ClipError(err.decode(errors="ignore")[-300:])
    return out


async def duration(path: str) -> float:
    out = await _run(config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", path, timeout=20)
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _recently_failed(key: str) -> bool:
    return time.time() - _failed.get(key, 0) < 3600


async def _download(url: str, dest: str, timeout: float) -> None:
    tmp = dest + ".part"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15),
                                     headers={"User-Agent": config.USER_AGENT},
                                     follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                if r.status_code != 200:
                    raise ClipError(f"HTTP {r.status_code}")
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes(65536):
                        fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def fetch_deezer(song) -> str:
    """Extrait Deezer (lien d'extrait signé redemandé à chaque fois : il expire)."""
    path = dz_path(song)
    if os.path.exists(path):
        return path
    key = f"dz:{song.id}"
    if not song.deezer_id or _recently_failed(key):
        raise ClipError("pas d'extrait Deezer")
    async with _locks.setdefault(key, asyncio.Lock()):
        if os.path.exists(path):
            return path
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"https://api.deezer.com/track/{song.deezer_id}")
                preview = (r.json() or {}).get("preview") if r.status_code == 200 else ""
            if not preview:
                raise ClipError("Deezer sans extrait")
            await _download(preview, path, timeout=30)
            return path
        except Exception as e:
            _failed[key] = time.time()
            raise ClipError(f"Deezer : {e}") from e


async def fetch_tv(song, timeout: float = 60) -> str:
    """Version TV AnimeThemes (.ogg) → mp3 en cache."""
    path = at_path(song)
    if os.path.exists(path):
        return path
    key = f"at:{song.id}"
    if not song.at_audio or _recently_failed(key):
        raise ClipError("pas de version TV")
    async with _locks.setdefault(key, asyncio.Lock()):
        if os.path.exists(path):
            return path
        raw = path + ".ogg"
        try:
            await _download(song.at_audio, raw, timeout=timeout)
            await _run(config.FFMPEG, "-y", "-v", "error", "-i", raw, "-vn", "-map_metadata", "-1",
                       "-ac", "2", "-b:a", "112k", "-f", "mp3", path + ".tmp", timeout=120)
            os.replace(path + ".tmp", path)
            return path
        except Exception as e:
            _failed[key] = time.time()
            raise ClipError(f"AnimeThemes : {e}") from e
        finally:
            for p in (raw, path + ".tmp"):
                if os.path.exists(p):
                    os.remove(p)


def playable(song) -> bool:
    """Jouable tout de suite en partie : version TV déjà en cache, ou extrait Deezer (CDN
    rapide). On ne télécharge jamais AnimeThemes pendant une partie (trop lent)."""
    return has_tv(song) or bool(song.deezer_id)


async def source_for(song, prefer: str) -> tuple[str, str]:
    """(fichier, type) selon la préférence : tv = version de l'anime, full = version
    complète (Deezer), auto = version TV si déjà en cache, sinon Deezer."""
    tv = at_path(song) if has_tv(song) else None
    if prefer == "full" and song.deezer_id:
        try:
            return await fetch_deezer(song), "full"
        except ClipError:
            if not tv:
                raise
    if tv:
        return tv, "tv"
    return await fetch_deezer(song), "full"


async def make_round_clip(song, seconds: int, start: str, prefer: str, rng) -> tuple[str, str, float]:
    """Découpe l'extrait d'une manche → (jeton, version, début en s)."""
    src, version = await source_for(song, prefer)
    total = await duration(src)
    room = max(0.0, total - seconds - 1)
    if start == "start" or room <= 0:
        offset = 0.0
    elif start == "middle":
        offset = room / 2
    else:
        offset = rng.uniform(0, room)
    token = secrets.token_urlsafe(18)
    out = os.path.join(ROUND_DIR, f"{token}.mp3")
    await _run(config.FFMPEG, "-y", "-v", "error", "-ss", f"{offset:.2f}", "-t", str(seconds + 2), "-i", src,
               "-vn", "-map_metadata", "-1", "-ac", "2", "-b:a", "128k",
               "-af", "afade=t=in:d=0.4", "-f", "mp3", out, timeout=60)
    return token, version, offset


def round_file(token: str) -> str | None:
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        return None
    path = os.path.join(ROUND_DIR, f"{token}.mp3")
    return path if os.path.exists(path) else None


def drop_round(token: str):
    path = round_file(token)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def purge_rounds(max_age: float = 6 * 3600):
    now = time.time()
    for name in os.listdir(ROUND_DIR):
        p = os.path.join(ROUND_DIR, name)
        if now - os.path.getmtime(p) > max_age:
            try:
                os.remove(p)
            except OSError:
                pass


async def precache_loop(get_wanted, stop: asyncio.Event):
    """Télécharge en fond les versions TV (AnimeThemes est lent : on s'y prend d'avance).
    `get_wanted()` donne les titres à avoir en priorité (ceux des listes des membres)."""
    while not stop.is_set():
        did = False
        for song in get_wanted():
            if stop.is_set():
                return
            if has_tv(song) or not song.at_audio or _recently_failed(f"at:{song.id}"):
                continue
            did = True
            try:
                await fetch_tv(song, timeout=90)
            except ClipError as e:
                log.info("préchargement %s : %s", song.title, e)
            await asyncio.sleep(1)
            break  # on relit la liste à chaque fois (priorités qui bougent)
        try:
            await asyncio.wait_for(stop.wait(), 2 if did else 120)
        except asyncio.TimeoutError:
            pass
