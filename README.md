# DWAblindtest

Blind test d'openings / endings d'anime en multijoueur → [blindtest.dwa.gold](https://blindtest.dwa.gold)

- Salons à code, partie en temps réel par WebSocket, classement.
- Réglages + presets (nombre de manches, durée, types OP/ED/inserts, difficulté).
- Difficulté calculée à partir de la notoriété des titres (taux de réussite AMQ, vues YouTube, popularité Kitsu, Deezer, AnimeThemes).
- Extraits mp3 découpés par ffmpeg (sans métadonnées, pour ne pas donner la réponse), version TV AnimeThemes préchargée sinon extrait Deezer.
- Connexion par SSO depuis [DWAnime](https://anime.dwa.gold) ; lit en lecture seule les bases du site anime et de DWAnisongs.

## Stack

Python · FastAPI · Uvicorn · SQLite · Jinja2 · WebSocket · ffmpeg

## Lancer en local

```bat
copy .env.example .env
python -m venv venv
venv\Scripts\pip install -r requirements.txt
run.bat
```

Puis http://127.0.0.1:8090. `mettre_a_jour_pi.bat` déploie le code sur le Raspberry Pi.
