"""Base du blind test : parties jouées (classement) et presets de réglages des membres."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Game(Base):
    __tablename__ = "games"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(8), default="")
    host_id = Column(String(32), default="")
    settings = Column(Text, default="{}")
    players = Column(Integer, default=0)
    rounds = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), default=utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)


class GamePlayer(Base):
    __tablename__ = "game_players"
    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, index=True, nullable=False)
    user_id = Column(String(32), index=True, nullable=False)
    name = Column(String(80), default="")
    score = Column(Integer, default=0)
    rank = Column(Integer, default=0)
    correct = Column(Integer, default=0)
    answered = Column(Integer, default=0)
    best_streak = Column(Integer, default=0)


class GameRound(Base):
    __tablename__ = "game_rounds"
    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, index=True, nullable=False)
    idx = Column(Integer, default=0)
    track_id = Column(Integer, default=0)
    root_id = Column(Integer, default=0)
    anime = Column(String(400), default="")
    song = Column(String(400), default="")
    kind = Column(String(8), default="")
    difficulty = Column(Integer, default=0)
    found = Column(Integer, default=0)       # nb de joueurs qui ont trouvé


class Preset(Base):
    __tablename__ = "presets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), index=True, nullable=False)
    name = Column(String(60), default="")
    settings = Column(Text, default="{}")
    created_at = Column(DateTime(timezone=True), default=utcnow)


def init_db():
    Base.metadata.create_all(engine)


def db_session():
    return SessionLocal()
