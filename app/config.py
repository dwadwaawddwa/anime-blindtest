"""Configuration chargée depuis l'environnement (.env)."""
import os
import secrets
import shutil

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8090").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'blindtest.db')}")

# Bases des autres sites, en LECTURE SEULE : listes (anime) et musiques (Anisongs).
ANIME_DB = os.getenv("ANIME_DB", "/home/pi/anime-site/anime.db")
MUSIC_DB = os.getenv("MUSIC_DB", "/home/pi/anime-music/music.db")

# Connexion via le site anime (SSO signé HMAC, client « blindtest »).
ANIME_SITE_URL = os.getenv("ANIME_SITE_URL", "https://anime.dwa.gold").rstrip("/")
MUSIC_SITE_URL = os.getenv("MUSIC_SITE_URL", "https://music.dwa.gold").rstrip("/")
SSO_SECRET = os.getenv("SSO_SECRET", "")
LOCAL_USER_ID = os.getenv("LOCAL_USER_ID", "").strip()  # dev : session ouverte d'office

# Extraits audio : cache local (le serveur audio d'AnimeThemes est souvent lent).
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(BASE_DIR, "cache"))
FFMPEG = os.getenv("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.getenv("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
PRECACHE = os.getenv("PRECACHE", "1") == "1"   # téléchargement de fond des extraits AnimeThemes

USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) dwa-blindtest/1.0 (+https://blindtest.dwa.gold)"
