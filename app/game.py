"""Moteur de partie : salons (code à 5 caractères), réglages, sélection des musiques,
manches synchronisées par WebSocket, points et sauvegarde des résultats.

Tout tourne dans la boucle asyncio du serveur (un seul process) : pas de verrous.
"""
import asyncio
import json
import logging
import random
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from . import clips
from .catalog import catalog, user_roots
from .db import Game, GamePlayer, GameRound, db_session, utcnow

log = logging.getLogger("blindtest.game")

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_PLAYERS = 20
KINDS = ("OP", "ED", "IN", "OST")
STATUSES = ("finished", "watching", "to_watch")

# --------------------------------------------------------------------------- réglages
DEFAULTS = {
    "kinds": ["OP", "ED"],        # types de musiques
    "pool": "common",             # common = vu par tous · any = vu par au moins un · host · all
    "statuses": ["finished", "watching"],
    "diff_min": 0,                # difficulté 0 (tout le monde connaît) → 100 (introuvable)
    "diff_max": 100,
    "rounds": 15,
    "seconds": 20,                # temps d'écoute / de réponse
    "clip_start": "random",       # start · middle · random
    "audio": "auto",              # auto · tv (version de l'anime) · full (version complète)
    "answer": "choices",          # choices (QCM) · search (barre de recherche)
    "choices": 4,
    "distractors": "similar",     # similar (même genre / époque) · lists (nos animes) · random
    "images": True,               # images des animes sur les réponses
    "hint_kind": False,           # afficher OP / ED pendant la manche
    "hint_year": False,           # afficher l'année pendant la manche
    "one_per_anime": True,        # une seule musique par anime dans la partie
    "speed_bonus": True,
    "difficulty_bonus": True,
    "streak_bonus": True,
    "wrong_penalty": 0,           # points perdus sur une mauvaise réponse
    "early_end": True,            # la manche s'arrête quand tout le monde a répondu
    "reveal": 7,                  # secondes d'affichage de la réponse
    "auto_next": True,            # sinon l'hôte lance chaque manche
}

SCHEMA = {
    "kinds": ("multi", KINDS), "statuses": ("multi", STATUSES),
    "pool": ("choice", ("common", "any", "host", "all")),
    "diff_min": ("int", 0, 100), "diff_max": ("int", 0, 100),
    "rounds": ("int", 3, 60), "seconds": ("int", 5, 60),
    "clip_start": ("choice", ("start", "middle", "random")),
    "audio": ("choice", ("auto", "tv", "full")),
    "answer": ("choice", ("choices", "search")), "choices": ("int", 2, 8),
    "distractors": ("choice", ("similar", "lists", "random")),
    "images": ("bool",), "hint_kind": ("bool",), "hint_year": ("bool",), "one_per_anime": ("bool",),
    "speed_bonus": ("bool",), "difficulty_bonus": ("bool",), "streak_bonus": ("bool",),
    "wrong_penalty": ("int", 0, 200), "early_end": ("bool",), "reveal": ("int", 3, 30),
    "auto_next": ("bool",),
}

PRESETS = {
    "classique": {"name": "Classique", "icon": "🎧", "desc": "OP + ED, 4 choix avec images, 15 manches",
                  "settings": {}},
    "decouverte": {"name": "Découverte", "icon": "🌱", "desc": "Les plus connues, 25 s, indice OP/ED",
                   "settings": {"diff_max": 45, "seconds": 25, "hint_kind": True, "rounds": 12}},
    "expert": {"name": "Expert", "icon": "🧠", "desc": "Barre de recherche, titres pointus, 15 s",
               "settings": {"answer": "search", "diff_min": 55, "seconds": 15,
                            "kinds": ["OP", "ED", "IN"], "images": False}},
    "sprint": {"name": "Sprint", "icon": "⚡", "desc": "10 manches de 8 s, la vitesse paie",
               "settings": {"rounds": 10, "seconds": 8, "reveal": 4}},
    "marathon": {"name": "Marathon", "icon": "🏃", "desc": "40 manches, OP + ED + inserts",
                 "settings": {"rounds": 40, "kinds": ["OP", "ED", "IN"], "reveal": 5}},
    "ost": {"name": "Bandes originales", "icon": "🎼", "desc": "Que des OST, 6 choix, 25 s",
            "settings": {"kinds": ["OST"], "choices": 6, "seconds": 25}},
    "endings": {"name": "Endings", "icon": "🌙", "desc": "Que des endings",
                "settings": {"kinds": ["ED"]}},
    "soiree": {"name": "Soirée", "icon": "🎉", "desc": "Tout ce qu'au moins un joueur a vu, 20 manches",
               "settings": {"pool": "any", "rounds": 20, "reveal": 8}},
    "chaos": {"name": "Chaos", "icon": "🌀", "desc": "Tous les types, 8 choix au hasard, n'importe quel passage",
              "settings": {"kinds": list(KINDS), "pool": "any", "distractors": "random", "choices": 8}},
    "hardcore": {"name": "Hardcore", "icon": "💀", "desc": "Recherche, 10 s, −50 si faux",
                 "settings": {"answer": "search", "seconds": 10, "wrong_penalty": 50, "diff_min": 40,
                              "images": False, "kinds": ["OP", "ED", "IN"]}},
}


def clean_settings(raw: dict, base: dict | None = None) -> dict:
    out = dict(base or DEFAULTS)
    for key, rule in SCHEMA.items():
        if key not in raw:
            continue
        v = raw[key]
        kind = rule[0]
        if kind == "bool":
            out[key] = bool(v)
        elif kind == "int":
            try:
                out[key] = max(rule[1], min(rule[2], int(v)))
            except (TypeError, ValueError):
                pass
        elif kind == "choice" and v in rule[1]:
            out[key] = v
        elif kind == "multi" and isinstance(v, list):
            vals = [x for x in rule[1] if x in v]
            if vals:
                out[key] = vals
    if out["diff_min"] > out["diff_max"]:
        out["diff_min"], out["diff_max"] = out["diff_max"], out["diff_min"]
    return out


def preset_settings(key: str) -> dict:
    return clean_settings(PRESETS[key]["settings"])


# --------------------------------------------------------------------------- état
@dataclass
class Player:
    uid: str
    name: str
    avatar: str
    score: int = 0
    correct: int = 0
    rounds: int = 0
    streak: int = 0
    best_streak: int = 0
    sockets: set = field(default_factory=set)
    gone_at: float = 0.0

    @property
    def connected(self) -> bool:
        return bool(self.sockets)

    def public(self) -> dict:
        return {"id": self.uid, "name": self.name, "avatar": self.avatar, "score": self.score,
                "correct": self.correct, "streak": self.streak, "connected": self.connected}


@dataclass
class Answer:
    choice: int
    label: str
    seconds: float
    correct: bool
    points: int = 0


@dataclass
class RoundState:
    song: object
    token: str
    version: str
    choices: list
    started: float = 0.0
    answers: dict = field(default_factory=dict)


def anime_card(a) -> dict:
    return {"id": a.id, "title": a.title, "english": a.english if a.english != a.title else "",
            "cover": a.cover, "year": a.year}


class Room:
    def __init__(self, code: str, host: str):
        self.code = code
        self.host = host
        self.players: dict[str, Player] = {}
        self.settings = dict(DEFAULTS)
        self.preset = "classique"
        self.phase = "lobby"          # lobby · preparing · loading · playing · reveal · finished
        self.idx = -1
        self.total = 0
        self.current: RoundState | None = None
        self.history: list[dict] = []
        self.task: asyncio.Task | None = None
        self.touched = time.time()
        self.rng = random.Random()
        self.loaded: set[str] = set()
        self.wake = asyncio.Event()
        self.last_result: dict | None = None
        self.notice = ""
        self.skip = False             # l'hôte a demandé la manche suivante
        self._pool_cache: tuple | None = None

    # ------------------------------------------------------------------ envoi
    async def _send(self, ws, msg: dict):
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def broadcast(self, msg: dict):
        for p in list(self.players.values()):
            for ws in list(p.sockets):
                await self._send(ws, msg)

    async def push_state(self):
        for p in list(self.players.values()):
            for ws in list(p.sockets):
                await self._send(ws, self.snapshot(p.uid))

    def snapshot(self, uid: str) -> dict:
        snap = {
            "t": "state", "code": self.code, "me": uid, "host": self.host, "phase": self.phase,
            "settings": self.settings, "preset": self.preset, "notice": self.notice,
            "players": [p.public() for p in self.ranked()],
            "pool": self.pool_info() if self.phase in ("lobby", "finished") else None,
            "round": None, "result": self.last_result if self.phase in ("reveal", "finished") else None,
        }
        rs = self.current
        if rs is not None and self.phase in ("loading", "playing", "reveal"):
            snap["round"] = self.round_payload(rs)
            snap["round"]["answered"] = list(rs.answers)
            if self.phase == "playing":
                snap["round"]["remaining"] = max(0.0, self.settings["seconds"] - (time.monotonic() - rs.started))
            mine = rs.answers.get(uid)
            snap["round"]["my_answer"] = mine.choice if mine else None
        return snap

    def ranked(self) -> list[Player]:
        return sorted(self.players.values(), key=lambda p: (-p.score, -p.correct, p.name.lower()))

    # ------------------------------------------------------------------ joueurs
    async def join(self, ws, user: dict) -> bool:
        p = self.players.get(user["id"])
        if p is None:
            if len(self.players) >= MAX_PLAYERS:
                await self._send(ws, {"t": "error", "msg": "Salon plein"})
                return False
            p = self.players[user["id"]] = Player(user["id"], user["name"], user["avatar"])
            self._pool_cache = None
        p.sockets.add(ws)
        if self.host not in self.players or not self.players[self.host].connected:
            self.host = p.uid
        self.touched = time.time()
        await self.push_state()
        return True

    async def leave(self, ws, uid: str):
        p = self.players.get(uid)
        if p is None:
            return
        p.sockets.discard(ws)
        if not p.connected:
            p.gone_at = time.time()
            if self.phase == "lobby":  # dans le salon d'attente, on part vraiment
                del self.players[uid]
                self._pool_cache = None
            if uid == self.host:
                nxt = next((q for q in self.players.values() if q.connected), None)
                if nxt:
                    self.host = nxt.uid
        self.wake.set()
        await self.push_state()

    def connected_uids(self) -> set[str]:
        return {p.uid for p in self.players.values() if p.connected}

    # ------------------------------------------------------------------ musiques possibles
    def pool_roots(self) -> tuple[set[int], list[str]]:
        """Animes retenus selon le mode + noms des joueurs sans liste."""
        mode = self.settings["pool"]
        if mode == "all":
            return set(catalog.by_root), []
        lists = {uid: user_roots(uid, self.settings["statuses"]) for uid in self.players}
        empty = [self.players[u].name for u, r in lists.items() if not r]
        if mode == "host":
            return lists.get(self.host, set()), empty
        filled = [r for r in lists.values() if r]
        if not filled:
            return set(), empty
        if mode == "any":
            return set().union(*filled), empty
        return set.intersection(*filled), empty

    def eligible(self, roots: set[int]) -> list:
        s = self.settings
        out, seen_song = [], set()
        for r in roots:
            for song in catalog.by_root.get(r, ()):
                if song.kind not in s["kinds"] or not (s["diff_min"] <= song.difficulty <= s["diff_max"]):
                    continue
                if not clips.playable(song):
                    continue
                if song.song_id:  # même chanson dans un film récap / une autre saison
                    if song.song_id in seen_song:
                        continue
                    seen_song.add(song.song_id)
                out.append(song)
        return out

    def pool_info(self) -> dict:
        key = (json.dumps(self.settings, sort_keys=True), tuple(sorted(self.players)), catalog.loaded_at)
        if self._pool_cache and self._pool_cache[0] == key:
            return self._pool_cache[1]
        roots, empty = self.pool_roots()
        songs = self.eligible(roots)
        by_kind = {k: 0 for k in KINDS}
        for s in songs:
            by_kind[s.kind] += 1
        animes = len({s.root for s in songs})
        possible = min(len(songs), animes) if self.settings["one_per_anime"] else len(songs)
        info = {"songs": len(songs), "animes": animes, "possible": possible, "by_kind": by_kind,
                "no_list": empty, "list_animes": len(roots), "tv_ready": sum(1 for s in songs if clips.has_tv(s))}
        self._pool_cache = (key, info)
        return info

    def pick_songs(self, n: int) -> list:
        """Tirage équilibré entre les types choisis (les OST sont bien plus nombreuses), une
        musique max par anime si demandé ; quelques réserves en cas d'extrait introuvable."""
        roots, _ = self.pool_roots()
        songs = self.eligible(roots)
        by_kind: dict[str, list] = {}
        for s in songs:
            by_kind.setdefault(s.kind, []).append(s)
        for lst in by_kind.values():
            self.rng.shuffle(lst)
        picked, used_roots = [], set()
        want = n + 8
        while len(picked) < want and any(by_kind.values()):
            kind = self.rng.choice([k for k, v in by_kind.items() if v])
            song = by_kind[kind].pop()
            if self.settings["one_per_anime"] and song.root in used_roots:
                continue
            used_roots.add(song.root)
            picked.append(song)
        return picked

    def build_choices(self, song) -> list:
        if self.settings["answer"] != "choices":
            return []
        pool = set()
        for uid in self.players:
            pool |= user_roots(uid, list(STATUSES))
        n = self.settings["choices"] - 1
        cards = [catalog.anime[song.root]] + catalog.distractors(
            song.root, n, self.settings["distractors"], pool, self.rng)
        self.rng.shuffle(cards)
        return [anime_card(a) for a in cards]

    def round_payload(self, rs: RoundState) -> dict:
        s, song = self.settings, rs.song
        return {
            "idx": self.idx, "total": self.total, "clip": f"/clip/{rs.token}.mp3", "seconds": s["seconds"],
            "answer": s["answer"], "images": s["images"], "choices": rs.choices,
            "hint_kind": song.kind if s["hint_kind"] else None,
            "hint_year": catalog.anime[song.root].year if s["hint_year"] else None,
        }

    # ------------------------------------------------------------------ déroulement
    async def start(self, uid: str):
        if uid != self.host or self.phase not in ("lobby", "finished"):
            return
        info = self.pool_info()
        if info["possible"] < 1:
            await self.broadcast({"t": "toast", "msg": "Aucune musique ne correspond à ces réglages", "err": True})
            return
        for p in self.players.values():
            p.score = p.correct = p.rounds = p.streak = p.best_streak = 0
        self.history, self.last_result = [], None
        self.task = asyncio.create_task(self.run())

    async def stop(self, uid: str):
        if uid != self.host or not self.task:
            return
        self.task.cancel()

    async def run(self):
        s = self.settings
        try:
            self.phase, self.notice = "preparing", "Préparation des extraits…"
            await self.push_state()
            queue = deque(self.pick_songs(s["rounds"]))
            self.total = min(s["rounds"], self.pool_info()["possible"])

            async def prepare():
                while queue:
                    song = queue.popleft()
                    try:
                        token, version, _ = await clips.make_round_clip(
                            song, s["seconds"], s["clip_start"], s["audio"], self.rng)
                        return RoundState(song=song, token=token, version=version, choices=self.build_choices(song))
                    except Exception as e:
                        log.info("extrait impossible (%s) : %s", song.title, e)
                return None

            upcoming = asyncio.create_task(prepare())
            played = 0
            for i in range(self.total):
                rs = await upcoming
                if rs is None:
                    break
                upcoming = asyncio.create_task(prepare()) if i + 1 < self.total else None
                self.idx = i
                await self.play_round(rs)
                played += 1
            if upcoming:
                upcoming.cancel()
            self.total = played
            await self.finish()
        except asyncio.CancelledError:
            self.phase, self.notice, self.current = "lobby", "", None
            await self.broadcast({"t": "toast", "msg": "Partie arrêtée par l'hôte"})
            await self.push_state()
        except Exception:
            log.exception("partie %s", self.code)
            self.phase, self.notice, self.current = "lobby", "", None
            await self.broadcast({"t": "toast", "msg": "Erreur pendant la partie, retour au salon", "err": True})
            await self.push_state()
        finally:
            if self.current:
                clips.drop_round(self.current.token)

    async def _wait(self, until: float, done) -> None:
        """Attend jusqu'à `until` (monotonic) ou que done() soit vrai."""
        while time.monotonic() < until and not done():
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), min(0.5, max(0.01, until - time.monotonic())))
            except asyncio.TimeoutError:
                pass

    async def play_round(self, rs: RoundState):
        s = self.settings
        prev = self.current
        self.current, self.loaded, self.last_result = rs, set(), None
        self.phase, self.notice = "loading", ""
        if prev:
            clips.drop_round(prev.token)
        await self.broadcast({"t": "round_load", **self.round_payload(rs)})
        # chacun télécharge l'extrait avant le départ (8 s max pour les retardataires)
        await self._wait(time.monotonic() + 8, lambda: self.connected_uids() <= self.loaded)
        self.phase = "playing"
        rs.started = time.monotonic()
        await self.broadcast({"t": "round_play", "idx": self.idx, "seconds": s["seconds"]})
        await self._wait(rs.started + s["seconds"] + 0.3,
                         lambda: s["early_end"] and self.connected_uids() <= set(rs.answers))
        self.score_round(rs)
        self.phase = "reveal"
        await self.broadcast({"t": "round_reveal", "result": self.last_result,
                              "players": [p.public() for p in self.ranked()]})
        if s["auto_next"] and self.idx + 1 < self.total:
            self.skip = False
            await self._wait(time.monotonic() + s["reveal"], lambda: self.skip)
        elif self.idx + 1 < self.total:
            self.skip = False
            await self._wait(time.monotonic() + 3600, lambda: self.skip)
        else:
            await asyncio.sleep(min(4, s["reveal"]))

    def answer(self, uid: str, idx: int, choice: int) -> bool:
        rs = self.current
        if self.phase != "playing" or rs is None or idx != self.idx or uid in rs.answers or uid not in self.players:
            return False
        elapsed = time.monotonic() - rs.started
        if elapsed > self.settings["seconds"] + 0.5:
            return False
        a = catalog.get(choice)
        rs.answers[uid] = Answer(choice=choice, label=a.title if a else "?", seconds=round(elapsed, 2),
                                 correct=catalog.same_franchise(choice, rs.song.root))
        self.wake.set()
        return True

    def points_for(self, ans: Answer, song, streak: int) -> int:
        s = self.settings
        if not ans.correct:
            return -s["wrong_penalty"]
        pts = 100.0
        if s["speed_bonus"]:
            pts += 100 * max(0.0, 1 - ans.seconds / s["seconds"])
        if s["difficulty_bonus"]:
            pts *= 1 + song.difficulty / 200      # jusqu'à +50 % sur les titres pointus
        if s["streak_bonus"] and streak > 1:
            pts += min(streak - 1, 5) * 10
        return round(pts)

    def score_round(self, rs: RoundState):
        song, a = rs.song, catalog.anime[rs.song.root]
        results = []
        for p in self.players.values():
            ans = rs.answers.get(p.uid)
            if ans is None and not p.connected:
                continue
            p.rounds += 1
            if ans and ans.correct:
                p.streak += 1
                p.best_streak = max(p.best_streak, p.streak)
                p.correct += 1
            else:
                p.streak = 0
            pts = self.points_for(ans, song, p.streak) if ans else 0
            if ans:
                ans.points = pts
            p.score = max(0, p.score + pts)
            results.append({"id": p.uid, "name": p.name, "avatar": p.avatar,
                            "answer": ans.label if ans else None, "correct": bool(ans and ans.correct),
                            "seconds": ans.seconds if ans else None, "points": pts, "streak": p.streak})
        results.sort(key=lambda r: (not r["correct"], r["seconds"] if r["seconds"] is not None else 999))
        found = sum(1 for r in results if r["correct"])
        self.last_result = {
            "idx": self.idx, "total": self.total,
            "anime": anime_card(a),
            "song": {"title": song.title, "artist": song.artist, "kind": song.kind, "slug": song.slug,
                     "season": song.season, "difficulty": song.difficulty, "version": rs.version,
                     "youtube": song.youtube_id, "root": song.root},
            "results": results, "found": found,
        }
        self.history.append({"track_id": song.id, "root": song.root, "anime": a.title,
                             "song": f"{song.slug} · {song.title} — {song.artist}", "kind": song.kind,
                             "difficulty": song.difficulty, "found": found})

    async def next(self, uid: str):
        if uid == self.host and self.phase == "reveal":
            self.skip = True
            self.wake.set()

    async def finish(self):
        self.phase, self.current, self.notice = "finished", None, ""
        ranking = self.ranked()
        game_id = self.save(ranking)
        rank_rows = [{**p.public(), "rank": i + 1, "rounds": p.rounds, "best_streak": p.best_streak}
                     for i, p in enumerate(ranking)]
        self.last_result = {"final": True, "ranking": rank_rows, "rounds": self.history, "game_id": game_id}
        await self.broadcast({"t": "game_end", **self.last_result})
        await self.push_state()

    def save(self, ranking: list[Player]) -> int | None:
        if not self.history:
            return None
        s = db_session()
        try:
            g = Game(code=self.code, host_id=self.host, settings=json.dumps(self.settings),
                     players=len(ranking), rounds=len(self.history), ended_at=utcnow())
            s.add(g)
            s.flush()
            for i, p in enumerate(ranking):
                s.add(GamePlayer(game_id=g.id, user_id=p.uid, name=p.name, score=p.score, rank=i + 1,
                                 correct=p.correct, answered=p.rounds, best_streak=p.best_streak))
            for i, h in enumerate(self.history):
                s.add(GameRound(game_id=g.id, idx=i, track_id=h["track_id"], root_id=h["root"], anime=h["anime"],
                                song=h["song"], kind=h["kind"], difficulty=h["difficulty"], found=h["found"]))
            s.commit()
            return g.id
        except Exception:
            log.exception("sauvegarde de la partie")
            s.rollback()
            return None
        finally:
            s.close()

    async def back_to_lobby(self, uid: str):
        if uid == self.host and self.phase == "finished":
            self.phase, self.last_result = "lobby", None
            for p in [p for p in self.players.values() if not p.connected]:
                del self.players[p.uid]
            self._pool_cache = None
            await self.push_state()

    async def kick(self, uid: str, target: str):
        if uid != self.host or target == uid or target not in self.players:
            return
        p = self.players.pop(target)
        for ws in list(p.sockets):
            await self._send(ws, {"t": "kicked"})
            try:
                await ws.close()
            except Exception:
                pass
        self._pool_cache = None
        await self.push_state()

    async def update_settings(self, uid: str, raw: dict, preset: str | None):
        if uid != self.host or self.phase not in ("lobby", "finished"):
            return
        if preset in PRESETS:
            self.settings, self.preset = preset_settings(preset), preset
        else:
            self.settings, self.preset = clean_settings(raw, self.settings), preset or "perso"
        await self.push_state()


# --------------------------------------------------------------------------- salons
rooms: dict[str, Room] = {}


def new_room(host: str) -> Room:
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(5))
        if code not in rooms:
            break
    room = rooms[code] = Room(code, host)
    return room


async def janitor(stop: asyncio.Event):
    """Ferme les salons abandonnés (plus personne de connecté depuis 15 min)."""
    while not stop.is_set():
        now = time.time()
        for code, room in list(rooms.items()):
            if room.connected_uids():
                room.touched = now
            elif now - room.touched > 900:
                if room.task:
                    room.task.cancel()
                rooms.pop(code, None)
        clips.purge_rounds()
        try:
            await asyncio.wait_for(stop.wait(), 60)
        except asyncio.TimeoutError:
            pass
