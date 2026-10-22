import asyncio
import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("tele_app", ROOT / "src/main.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class Response:
    def __init__(self, status, body=None):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.body


class Session:
    def __init__(self, status=204, body=None, legacy_body=None):
        self.delete = Mock(return_value=Response(status, body))
        self.get = Mock(return_value=Response(200, legacy_body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    db = sqlite3.connect(path)
    app.init_db(db)
    monkeypatch.setattr(
        app, "DISCORD_WEBHOOK_URL", "https://discord.invalid/api/webhooks/333/new"
    )
    monkeypatch.setattr(app, "DISCORD_KEEP_MESSAGES", 1)
    monkeypatch.setattr(app, "DISCORD_MESSAGE_MAX_AGE_HOURS", 0)
    monkeypatch.setattr(app, "DISCORD_DELETE_INTERVAL_SECONDS", 0)

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)
    now = datetime.now(timezone.utc)
    with db:
        db.executemany(
            "INSERT INTO discord_messages VALUES (?,?,?)",
            [
                (
                    "100",
                    (now - timedelta(hours=25)).isoformat(),
                    "https://discord.invalid/api/webhooks/222/old",
                ),
                ("101", now.isoformat(), app.DISCORD_WEBHOOK_URL),
            ],
        )
    yield db
    db.close()


def ids(db):
    return [
        r[0]
        for r in db.execute(
            "SELECT message_id FROM discord_messages ORDER BY message_id"
        )
    ]


@pytest.mark.parametrize(
    "status,body", [(404, {"code": 10015}), (404, None), (503, None)]
)
def test_async_failures_keep_id_and_do_not_checkpoint(
    database, monkeypatch, status, body
):
    session = Session(status, body)
    monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **kw: session)
    asyncio.run(app.cleanup_discord_messages_once(database))
    first = session.delete.call_count
    assert ids(database) == ["100", "101"]
    assert app.get_meta(database, app.META_DISCORD_CLEANUP_KEY) is None
    asyncio.run(app.cleanup_discord_messages_once(database))
    assert session.delete.call_count > first


def test_async_cleanup_uses_original_route(database, monkeypatch):
    session = Session()
    monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **kw: session)
    asyncio.run(app.cleanup_discord_messages_once(database))
    assert ids(database) == ["101"]
    assert "/webhooks/222/old/messages/100" in session.delete.call_args.args[0]


def test_async_cleanup_migrates_proven_legacy_route(database, monkeypatch):
    with database:
        database.execute(
            'UPDATE discord_messages SET webhook_url=NULL WHERE message_id="100"'
        )
    session = Session(legacy_body={"id": "100", "webhook_id": "333"})
    monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **kw: session)
    asyncio.run(app.cleanup_discord_messages_once(database))
    assert ids(database) == ["101"]
    assert "/webhooks/333/new/messages/100" in session.delete.call_args.args[0]


def test_async_legacy_route_is_not_guessed(database, monkeypatch):
    with database:
        database.execute(
            'UPDATE discord_messages SET webhook_url=NULL WHERE message_id="100"'
        )
    session = Session(legacy_body={"id": "100", "webhook_id": "different"})
    monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **kw: session)
    asyncio.run(app.cleanup_discord_messages_once(database))
    assert ids(database) == ["100", "101"]
    session.delete.assert_not_called()


def test_async_expiry_below_count_limit(database, monkeypatch):
    monkeypatch.setattr(app, "DISCORD_KEEP_MESSAGES", 50)
    monkeypatch.setattr(app, "DISCORD_MESSAGE_MAX_AGE_HOURS", 24)
    session = Session()
    monkeypatch.setattr(app.aiohttp, "ClientSession", lambda **kw: session)
    asyncio.run(app.cleanup_discord_messages_once(database))
    assert ids(database) == ["101"]


def test_async_sender_tracks_id_in_persistent_database(database):
    app.add_discord_message(database, "999")
    assert "999" in ids(database)
