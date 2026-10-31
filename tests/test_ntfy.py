import json

import httpx
import pytest

from smsmonster.webhooks import WebhookDispatcher, ntfy_message, split_ntfy_url


def test_split_ntfy_url():
    assert split_ntfy_url("https://ntfy.sh/alerts") == ("https://ntfy.sh/", "alerts")
    assert split_ntfy_url("http://ntfy.lan:8080/sms/") == ("http://ntfy.lan:8080/", "sms")
    assert split_ntfy_url("https://ntfy.sh") == ("https://ntfy.sh/", "")


def test_ntfy_message_shapes():
    m = ntfy_message("message.received", {"number": "+4917012", "text": "Hällo ✓"})
    assert m["title"] == "SMS from +4917012" and m["message"] == "Hällo ✓"
    f = ntfy_message("message.failed", {"number": "+1", "text": "hi", "error": "CMS 500"})
    assert f["priority"] == 4 and "CMS 500" in f["message"]
    assert ntfy_message("call.received", {"number": "+2", "modem_id": "x"})["title"] == "Call from +2"
    assert "SMS Monster" in ntfy_message("test", {"message": "ping"})["title"]


@pytest.fixture
def capture():
    seen = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    return seen, httpx.MockTransport(handler)


async def _run(db, capture, secret):
    seen, transport = capture
    db.add_webhook("https://ntfy.sh/alerts", ["*"], secret, type="ntfy")
    d = WebhookDispatcher(db)
    d._client = httpx.AsyncClient(transport=transport)
    await d._deliver(db.list_webhooks()[0], "message.received", {"number": "+49170", "text": "Code 1234 ✓"})
    await d.close()
    assert len(seen) == 1
    return seen[0]


async def test_ntfy_delivery_json_publish(db, capture):
    req = await _run(db, capture, "tk_secret")
    assert str(req.url) == "https://ntfy.sh/"
    assert req.headers["Authorization"] == "Bearer tk_secret"
    body = json.loads(req.content)
    assert body == {"topic": "alerts", "title": "SMS from +49170", "message": "Code 1234 ✓", "tags": ["envelope"]}


async def test_ntfy_basic_auth(db, capture):
    req = await _run(db, capture, "bob:pw")
    assert req.headers["Authorization"] == "Basic Ym9iOnB3"


async def test_ntfy_without_topic_is_skipped(db, capture):
    seen, transport = capture
    db.add_webhook("https://ntfy.sh", ["*"], None, type="ntfy")
    d = WebhookDispatcher(db)
    d._client = httpx.AsyncClient(transport=transport)
    await d._deliver(db.list_webhooks()[0], "test", {"message": "x"})
    await d.close()
    assert seen == []


def test_webhook_type_migration(tmp_path):
    import sqlite3

    from smsmonster.db import Database
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE webhooks (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, secret TEXT, events TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
    c.execute("INSERT INTO webhooks (url, events, created_at) VALUES ('http://old', '[\"*\"]', 'now')")
    c.commit(); c.close()
    db = Database(str(path))
    assert db.list_webhooks()[0]["type"] == "webhook"
    db.close()
