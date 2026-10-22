"""Catalogue en mémoire, relu toutes les 5 min depuis les bases du site anime et d'Anisongs
(ouvertes en LECTURE SEULE) : animes (= franchises), musiques jouables et leur difficulté."""
import logging
import math
import random
import re
import sqlite3
import threading
import time
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("blindtest.catalog")


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class Anime:
    id: int                       # id Kitsu de l'anime racine (comme le site anime)
    title: str
    english: str
    cover: str
    year: int
    popularity: int
    group_key: str
    genres: set = field(default_factory=set)
    search: str = ""              # tous les titres de la franchise, normalisés


@dataclass
class Song:
    id: int                       # id du titre dans Anisongs
    root: int                     # franchise
    kind: str                     # OP / ED / IN / OST
    slug: str
    title: str
    artist: str
    season: str                   # saison / film / album d'où vient le titre
    year: int
    song_id: int | None
    at_audio: str
    deezer_id: int | None
    youtube_id: str
    difficulty: int = 50          # 0 = tout le monde connaît, 100 = introuvable


KIND_EASE = {"OP": 0.7, "ED": 0.5, "IN": 0.3, "OST": 0.2}
# poids des indices de notoriété dans la difficulté
WEIGHTS = {"amq": 3.0, "yt": 2.0, "pop": 1.5, "dz": 1.0, "at": 1.0, "kind": 1.0}


def _ro(path: str):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _percentiles(values: dict[int, float]) -> dict[int, float]:
    """id → rang centile (0..1) de sa valeur parmi toutes."""
    if not values:
        return {}
    ordered = sorted(values.values())
    n = len(ordered)
    return {k: bisect_left(ordered, v) / max(1, n - 1) for k, v in values.items()}


class Catalog:
    def __init__(self):
        self.anime: dict[int, Anime] = {}
        self.songs: dict[int, Song] = {}
        self.by_root: dict[int, list[Song]] = {}
        self.loaded_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ chargement
    def load(self):
        anime, root_of = self._load_anime()
        songs = self._load_songs(anime, root_of)
        by_root: dict[int, list[Song]] = {}
        for s in songs.values():
            by_root.setdefault(s.root, []).append(s)
        with self._lock:
            self.anime, self.songs, self.by_root = anime, songs, by_root
            self.loaded_at = time.time()
        log.info("catalogue : %d animes, %d musiques jouables", len(anime), len(songs))

    def _load_anime(self):
        with _ro(config.ANIME_DB) as c:
            rows = c.execute("""select id, romaji, english, native, synonyms, search_blob, cover_url, genres,
                                       season_year, popularity, root_id, group_key from anime""").fetchall()
        groups: dict[int, list] = {}
        for r in rows:
            groups.setdefault(r["root_id"] or r["id"], []).append(r)
        anime, root_of = {}, {}
        for root, members in groups.items():
            head = next((m for m in members if m["id"] == root), members[0])
            genres = {g.strip() for m in members for g in (m["genres"] or "").split(",") if g.strip()}
            blob = " ".join(fold(" ".join(filter(None, (m["romaji"], m["english"], m["native"],
                                                        (m["synonyms"] or "").replace("\n", " ")))))
                            for m in members)
            years = [m["season_year"] for m in members if m["season_year"]]
            anime[root] = Anime(
                id=root, title=head["romaji"] or head["english"] or head["native"] or f"#{root}",
                english=head["english"] or "", cover=head["cover_url"] or "",
                year=min(years) if years else 0,
                popularity=max((m["popularity"] or 0) for m in members),
                group_key=head["group_key"] or f"#{root}", genres=genres, search=blob)
            for m in members:
                root_of[m["id"]] = root
        return anime, root_of

    def _load_songs(self, anime: dict[int, Anime], root_of: dict[int, int]) -> dict[int, Song]:
        with _ro(config.MUSIC_DB) as c:
            at_root: dict[int, int] = {}
            for f in c.execute("select root_id, at_ids from franchises"):
                for x in (f["at_ids"] or "").split(","):
                    if x.strip().lstrip("-").isdigit():
                        at_root.setdefault(int(x), f["root_id"])
            at_info = {r["id"]: r for r in c.execute("select id, name, year from at_anime")}
            albums = {r["id"]: r for r in c.execute("select id, root_id, title, release_date from albums")}
            rows = c.execute("""select id, kind, slug, title, artist, at_anime_id, album_id, song_id, at_audio,
                                       deezer_id, youtube_id, amq_rate, at_plays, deezer_rank, yt_views
                                from tracks
                                where (at_audio != '' or deezer_id is not null)""").fetchall()
        songs: dict[int, Song] = {}
        signals: dict[str, dict[int, float]] = {k: {} for k in ("amq", "yt", "dz", "at", "pop")}
        for r in rows:
            if r["album_id"]:
                al = albums.get(r["album_id"])
                if al is None:
                    continue
                root, season = al["root_id"], al["title"]
                year = int((al["release_date"] or "0")[:4] or 0)
            else:
                root = at_root.get(r["at_anime_id"])
                info = at_info.get(r["at_anime_id"])
                season, year = (info["name"], info["year"] or 0) if info else ("", 0)
            root = root_of.get(root, root)
            if root not in anime:
                continue
            s = Song(id=r["id"], root=root, kind=r["kind"], slug=r["slug"] or r["kind"], title=r["title"] or "",
                     artist=r["artist"] or "", season=season or "", year=year, song_id=r["song_id"],
                     at_audio=r["at_audio"] or "", deezer_id=r["deezer_id"], youtube_id=r["youtube_id"] or "")
            songs[s.id] = s
            if r["amq_rate"] is not None:
                signals["amq"][s.id] = r["amq_rate"] / 100
            if (r["yt_views"] or 0) > 0:
                signals["yt"][s.id] = math.log10(r["yt_views"])
            if (r["deezer_rank"] or 0) > 0:
                signals["dz"][s.id] = r["deezer_rank"]
            if (r["at_plays"] or 0) > 0:
                signals["at"][s.id] = math.log10(1 + r["at_plays"])
            if anime[root].popularity:
                signals["pop"][s.id] = math.log10(1 + anime[root].popularity)
        # notoriété = moyenne pondérée des indices disponibles (centiles, le taux AMQ tel quel)
        pct = {k: (v if k == "amq" else _percentiles(v)) for k, v in signals.items()}
        ease = {}
        for sid, s in songs.items():
            tot = w = 0.0
            for k, weight in WEIGHTS.items():
                v = KIND_EASE.get(s.kind, 0.3) if k == "kind" else pct[k].get(sid)
                if v is not None:
                    tot += weight * v
                    w += weight
            ease[sid] = tot / w
        # difficulté = centile inverse de la notoriété : répartie de 0 à 100 sur tout le catalogue
        rank = _percentiles({sid: -e for sid, e in ease.items()})
        for sid, s in songs.items():
            s.difficulty = round(100 * rank[sid])
        return songs

    # ------------------------------------------------------------------ requêtes
    def get(self, root: int) -> Anime | None:
        return self.anime.get(root)

    def search(self, q: str, limit: int = 8) -> list[Anime]:
        toks = fold(q).split()
        if not toks:
            return []
        hits = [a for a in self.anime.values() if all(t in a.search for t in toks)]
        # titre qui commence par la saisie d'abord, puis les plus populaires
        qf = fold(q)
        hits.sort(key=lambda a: (not fold(a.title).startswith(qf) and not fold(a.english).startswith(qf),
                                 -a.popularity))
        return hits[:limit]

    def same_franchise(self, a: int, b: int) -> bool:
        x, y = self.anime.get(a), self.anime.get(b)
        return a == b or (x is not None and y is not None and x.group_key == y.group_key)

    def distractors(self, answer: int, n: int, mode: str, pool: set[int], rng: random.Random) -> list[Anime]:
        """Mauvaises réponses plausibles. « similar » : mêmes genres et même époque, en
        priorité parmi les animes des joueurs (sinon on devine par élimination)."""
        ans = self.anime[answer]
        cands = [a for a in self.anime.values() if a.group_key != ans.group_key and a.cover]
        if mode == "lists":
            mine = [a for a in cands if a.id in pool]
            cands = mine if len(mine) >= n else cands
        if mode == "random":
            known = [a for a in cands if a.id in pool or a.popularity > 0]
            rng.shuffle(known)
            picks = known[:n]
        else:
            def score(a: Anime) -> float:
                inter = len(a.genres & ans.genres)
                union = len(a.genres | ans.genres) or 1
                era = 1 - min(abs((a.year or ans.year) - ans.year), 20) / 20 if ans.year else 0.5
                return 2 * inter / union + era + (0.6 if a.id in pool else 0) + rng.random() * 0.6
            ranked = sorted(cands, key=score, reverse=True)
            top = ranked[:max(n * 4, 12)]
            rng.shuffle(top)
            picks = top[:n]
        out, seen = [], {ans.group_key}
        for a in picks:
            if a.group_key not in seen:
                seen.add(a.group_key)
                out.append(a)
        return out


catalog = Catalog()


def user_roots(uid: str, statuses: list[str]) -> set[int]:
    """Animes (racines) de la liste d'un membre, pour les statuts demandés."""
    if not statuses:
        return set()
    marks = ",".join("?" * len(statuses))
    with _ro(config.ANIME_DB) as c:
        rows = c.execute(f"select e.anime_id, a.root_id from entries e join anime a on a.id = e.anime_id "
                         f"where e.user_id = ? and e.status in ({marks})", (uid, *statuses)).fetchall()
    return {r["root_id"] or r["anime_id"] for r in rows}


def user_info(uid: str) -> dict | None:
    with _ro(config.ANIME_DB) as c:
        r = c.execute("select id, username, global_name, avatar, custom_avatar from users where id = ?",
                      (uid,)).fetchone()
    if r is None:
        return None
    name = r["global_name"] or r["username"] or "?"
    if r["custom_avatar"]:
        avatar = f"{config.ANIME_SITE_URL}/uploads/{uid}.jpg?v={r['custom_avatar']}"
    elif not str(uid).isdigit():
        avatar = f"{config.ANIME_SITE_URL}/static/avatar-default.svg"
    elif r["avatar"]:
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{r['avatar']}.png?size=64"
    else:
        avatar = f"https://cdn.discordapp.com/embed/avatars/{int(uid) % 5}.png"
    return {"id": uid, "name": name, "avatar": avatar}


def refresher(stop: threading.Event):
    while not stop.is_set():
        try:
            catalog.load()
        except Exception:
            log.exception("chargement du catalogue")
        stop.wait(300)
