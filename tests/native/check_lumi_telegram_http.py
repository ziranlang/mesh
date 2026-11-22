"""Exercise the native Lumi Telegram handler with disposable SQLite and loopback providers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time

from check_feedback_http import login, node, request

BASE = "/api/v1/lumi/telegram/"
BOT = "345:fixture-token"
SECRET_NAME = "X-Telegram-Bot-Api-Secret-Token"
SECRET = "fixture-webhook-secret"
WEBHOOK = {SECRET_NAME: SECRET}
CANVAS = "https://example.test/lumi"
CLIENT = "a" * 64
OTHER_CLIENT = "b" * 64
PRIVATE = 1234
GROUP = -4211
NO_UPDATE = {"id": 0, "kind": "", "text": "", "choice": 0}


@contextmanager
def fake_provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, self.headers.get("Authorization")))
            parameters = body.get("reply_parameters", {})
            original = parameters.get("message_id")
            if original == 204:
                self.close_connection = True
                return
            if self.path == "/":
                value = {"choices": [{"message": {"role": "assistant", "content": "Try a short breathing practice."}}]}
                status = 200
            elif original == 705:
                value = {"ok": False, "error_code": 400, "description": "reply unavailable"}
                status = 400
            else:
                value = {"ok": True, "result": {"message_id": 9000 + (original or 0)}}
                status = 200
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port, calls
        finally:
            server.shutdown()
            thread.join(timeout=5)


def bot_calls(calls, method):
    return [body for path, body, _ in calls if path == "/bot" + BOT + "/" + method]


def issue(port, owner_auth):
    url = request(port, "POST", BASE + "link", headers=owner_auth)["url"]
    assert re.fullmatch(r"https://t\.me/inlumi_bot\?start=[0-9a-f]{32}", url), url
    return url.rsplit("=", 1)[1]


def message(id, text, *, sender=PRIVATE, chat=PRIVATE, kind="private", original=None, thread=0, reply_bot=0, bot=False):
    return {"update_id": id, "message": {"message_id": id if original is None else original,
            "from": {"id": sender, "is_bot": bot}, "chat": {"id": chat, "type": kind}, "text": text,
            "message_thread_id": thread, "reply_to_message": {"from": {"id": reply_bot}}}}


def webhook(port, payload, *, headers=WEBHOOK, expected=200):
    return request(port, "POST", BASE + "webhook", headers=headers, body=payload, expected=expected)


def poll(port, account, client=CLIENT):
    return request(port, "GET", BASE + "updates?client=" + client, headers=account)


def ack(port, account, id, *, client=CLIENT, reply=None, expected=200):
    body = {"id": id, "client": client}
    if reply is not None:
        body["reply"] = reply
    return request(port, "POST", BASE + "ack", headers=account, body=body, expected=expected)


def check_disabled(port, oracle, calls):
    owner, owner_auth = login(port, oracle, "lumi-telegram-owner")
    other, other_auth = login(port, oracle, "lumi-telegram-other")
    assert request(port, "GET", BASE + "link", expected=405) is None
    for method, path in (("POST", "link"), ("DELETE", "link"), ("GET", "updates"), ("POST", "ack")):
        assert request(port, method, BASE + path, headers=owner_auth, expected=503) == {"error": "Lumi Telegram unavailable"}
    assert webhook(port, message(1, "Hello"), expected=401) == {"error": "Telegram authentication required"}
    body = {"id": "local.history", "text": "Saved app conversation", "draft": "d" * 32}
    assert request(port, "POST", BASE + "messages", body=body, expected=401) == {"error": "bearer token required"}
    assert request(port, "POST", BASE + "messages", headers=owner_auth, body=body) is True
    assert request(port, "POST", BASE + "messages", headers=owner_auth | {"X-Mesh-User": other}, body=body,
                   expected=401) == {"error": "token user mismatch"}
    raw = json.dumps(body).encode().ljust(16384)
    assert request(port, "POST", BASE + "messages", headers=owner_auth, body=raw) is True
    assert request(port, "POST", BASE + "messages", headers=owner_auth, body=raw + b" ", expected=400) == {
        "error": "invalid message",
    }
    assert not calls
    return owner, owner_auth, other, other_auth


def legacy_tables(database, owner):
    with closing(sqlite3.connect(database)) as storage, storage:
        storage.executescript("""
            CREATE TABLE server_lumi_link_codes(code TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES server_users(user_id_hash) ON DELETE CASCADE, expires INTEGER NOT NULL);
            CREATE TABLE server_lumi_updates(id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES server_users(user_id_hash) ON DELETE CASCADE, kind TEXT NOT NULL,
                text TEXT NOT NULL,choice INTEGER NOT NULL DEFAULT 0,done INTEGER NOT NULL DEFAULT 0,
                executor TEXT NOT NULL DEFAULT '');
        """)
        storage.execute("INSERT INTO server_lumi_link_codes VALUES(?,?,?)", ("c" * 32, owner, int(time.time()) + 600))
        storage.execute("INSERT INTO server_lumi_updates(id,account_id,kind,text,done) VALUES(?,?,?,?,1)",
                        (7777, owner, "message", "handled before upgrade"))


def check_link(port, database, owner_auth, other_auth, calls):
    assert request(port, "POST", BASE + "link", expected=401) == {"error": "bearer token required"}
    assert request(port, "GET", BASE + "updates?client=short", headers=owner_auth, expected=400) == {"error": "app client required"}
    url_code = issue(port, owner_auth)
    with closing(sqlite3.connect(database)) as storage:
        expires, = storage.execute("SELECT expires FROM server_lumi_link_codes WHERE code=?", (url_code,)).fetchone()
        assert 595 <= expires - int(time.time()) <= 600
        assert storage.execute("SELECT 1 FROM server_lumi_updates WHERE id=7777").fetchone() is None
    start = message(1, "/start " + url_code)
    assert webhook(port, start, headers={}, expected=401) == {"error": "Telegram authentication required"}
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as client:
        body = json.dumps(start).encode()
        client.putrequest("POST", BASE + "webhook")
        client.putheader(SECRET_NAME, SECRET)
        client.putheader(SECRET_NAME, SECRET)
        client.putheader("Content-Length", str(len(body)))
        client.endheaders(body)
        response = client.getresponse()
        assert response.status == 401
        response.read()
    assert webhook(port, {"update_id": "1"}, expected=400) == {"error": "invalid update"}
    assert webhook(port, message(1, "/start " + url_code, chat=333)) is True
    assert webhook(port, message(1, "/start " + url_code, kind="group", chat=-1)) is True
    assert webhook(port, message(1, "/start " + url_code, bot=True)) is True
    assert not calls
    assert poll(port, owner_auth) == {"linked": False, "linked_at": 0, "update": NO_UPDATE}
    assert webhook(port, message(2, "/start " + "c" * 32)) == {"link_status": "connected"}
    assert bot_calls(calls, "sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Open Inner Breeze", "web_app": {"url": CANVAS},
    }
    assert webhook(port, start) == {"link_status": "connected"}
    assert webhook(port, start) == {"link_status": "reused"}
    expired = issue(port, owner_auth)
    with closing(sqlite3.connect(database)) as storage, storage:
        storage.execute("UPDATE server_lumi_link_codes SET expires=0 WHERE code=?", (expired,))
    assert webhook(port, message(3, "/start " + expired)) == {"link_status": "expired"}
    competing = issue(port, other_auth)
    assert webhook(port, message(4, "/start " + competing)) == {"link_status": "conflicting_account"}
    assert not poll(port, other_auth)["linked"]
    assert webhook(port, message(4, "/start " + competing, sender=5678, chat=5678)) == {"link_status": "connected"}
    assert poll(port, other_auth)["linked"]
    a = issue(port, owner_auth)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda i: webhook(port, message(i, "/start " + a))["link_status"], (5, 6)))
    assert sorted(claims) == ["connected", "reused"]
    state = poll(port, owner_auth)
    assert state["linked"] and state["linked_at"] > 0 and state["update"] == NO_UPDATE
    return expired


def check_private(port, database, owner_auth, other_auth, calls):
    first = len(calls)
    assert webhook(port, message(10, "/todo@other_bot text")) is True
    assert webhook(port, message(11, "")) is True
    assert webhook(port, message(12, "a" * 1024)) is True
    assert len(calls) == first
    assert webhook(port, message(13, "/help@INLUMI_BOT")) is True
    assert "/feedback" in bot_calls(calls, "sendMessage")[-1]["text"]
    assert webhook(port, message(14, "/todo \t")) is True
    assert bot_calls(calls, "sendMessage")[-1]["text"] == "Use /todo <task title>"
    assert webhook(port, message(15, "/madeup")) is True
    assert "Unknown command" in bot_calls(calls, "sendMessage")[-1]["text"]
    assert webhook(port, message(16, "/history")) is True
    assert "full Lumi history" in bot_calls(calls, "sendMessage")[-1]["text"]
    assert webhook(port, message(17, "/paysupport")) is True
    assert "Telegram Stars purchase" in bot_calls(calls, "sendMessage")[-1]["text"]
    assert webhook(port, message(18, "/start auth_" + "1" * 32)) is True
    assert "unavailable or expired" in bot_calls(calls, "sendMessage")[-1]["text"]

    sent = "/JOURNAL@inlumi_bot: A calm morning\n水を飲む  "
    assert webhook(port, message(202, sent)) is True
    assert webhook(port, message(202, "different duplicate")) is True
    assert len(bot_calls(calls, "sendChatAction")) == 1
    assert ack(port, owner_auth, 202, reply="unclaimed", expected=409) == {"error": "no matching Telegram message claim"}
    assert poll(port, owner_auth)["update"] == {
        "id": 202, "kind": "message", "text": "/journal: A calm morning\n水を飲む  ", "choice": 0, "can_reply": True,
    }
    assert poll(port, owner_auth, OTHER_CLIENT)["update"] == NO_UPDATE
    assert poll(port, other_auth)["update"] == NO_UPDATE
    assert ack(port, other_auth, 202, reply="wrong account", expected=409) == {"error": "no matching Telegram message claim"}
    assert ack(port, owner_auth, 202, client=OTHER_CLIENT, reply="wrong device", expected=409) == {"error": "no matching Telegram message claim"}
    assert ack(port, owner_auth, 202, client=OTHER_CLIENT) is True
    assert poll(port, owner_auth)["update"]["id"] == 202
    assert ack(port, owner_auth, -202, reply="not Telegram", expected=400) == {"error": "invalid receipt"}
    assert ack(port, owner_auth, 202, reply="é" * 2049, expected=400) == {"error": "invalid receipt"}
    assert ack(port, owner_auth, 202, reply="A quiet morning 水") is True
    assert ack(port, owner_auth, 202, reply="retry") is True
    direct = [item for item in bot_calls(calls, "sendMessage") if "reply_parameters" in item]
    assert direct == [{"chat_id": PRIVATE, "text": "A quiet morning 水", "reply_parameters": {
        "message_id": 202, "allow_sending_without_reply": False,
    }}]
    assert webhook(port, message(204, "another private question")) is True
    assert poll(port, owner_auth)["update"]["id"] == 204
    assert ack(port, owner_auth, 204, reply="uncertain", expected=202) is True
    assert ack(port, owner_auth, 204, reply="do not replay", expected=202) is True
    assert sum(body.get("reply_parameters", {}).get("message_id") == 204 for body in bot_calls(calls, "sendMessage")) == 1

    confirmation = {"update_id": 205, "callback_query": {"id": "choice", "data": "Y" + "9" * 32,
                    "from": {"id": PRIVATE}, "message": {"chat": {"id": PRIVATE, "type": "private"}}}}
    assert webhook(port, confirmation) is True
    assert webhook(port, confirmation) is True
    assert bot_calls(calls, "answerCallbackQuery") == [{"callback_query_id": "choice"}]
    assert poll(port, owner_auth)["update"] == {"id": 205, "kind": "confirmation", "text": "9" * 32, "choice": 1}
    assert ack(port, owner_auth, 205, reply="not replyable", expected=409) == {"error": "no matching Telegram message claim"}
    assert ack(port, owner_auth, 205) is True
    assert poll(port, owner_auth)["update"] == NO_UPDATE
    with closing(sqlite3.connect(database)) as storage:
        assert storage.execute("SELECT text,telegram_reply_id,done FROM server_lumi_updates WHERE id=202").fetchone() == ("", 9202, 1)
        assert storage.execute("SELECT text,telegram_reply_id,done FROM server_lumi_updates WHERE id=204").fetchone() == ("", 0, 1)


def check_groups(port, database, calls):
    def group(id, text, *, chat=GROUP, **kwargs):
        return webhook(port, message(id, text, chat=chat, kind="supergroup", **kwargs))

    before = len(calls)
    for id, text in enumerate(("luminous colors", "the weather is nice", "How do habits work?", "I spoke to Lumi yesterday",
                               "/help@other_bot Inner Breeze", "person@inlumi_bot hello"), 650):
        assert group(id, text) is True
    assert group(660, "How do I use Inner Breeze?", chat=GROUP - 1) is True
    assert group(661, "hi Lumi", bot=True) is True
    assert len(calls) == before
    assert group(700, "where do you live lumi?", thread=71) is True
    local = bot_calls(calls, "sendMessage")[-1]
    assert "physical home" in local["text"] and local["message_thread_id"] == 71
    assert local["reply_parameters"] == {"message_id": 700, "allow_sending_without_reply": True}
    after_local = len(calls)
    assert group(700, "hi Lumi") is True
    assert group(701, "hi Lumi", original=700) is True
    assert len(calls) == after_local
    assert group(702, "Lumi, show my Inner Breeze diary") is True
    assert "can't access" in bot_calls(calls, "sendMessage")[-1]["text"]
    assert group(703, "How do I use Inner Breeze?", thread=73) is True
    assert group(704, "how do habits work?", reply_bot=345) is True
    assert group(705, "Hi Lumi") is True
    before_retry = len(calls)
    assert group(705, "Hi Lumi") is True
    assert len(calls) == before_retry
    assert group(706, "How do I use Inner Breeze for breathing?") is True
    assert "allowance" in bot_calls(calls, "sendMessage")[-1]["text"]
    models = [(body, auth) for path, body, auth in calls if path == "/"]
    assert len(models) == 2
    for body, auth in models:
        assert auth == "Bearer fixture-model-key"
        assert body["max_tokens"] == 1024 and body["thinking"] == {"type": "enabled"}
        assert [item["role"] for item in body["messages"]] == ["system", "user"]
        assert "no account access" in body["messages"][0]["content"] and "tools" not in body
    with closing(sqlite3.connect(database)) as storage:
        assert storage.execute("SELECT account_id,used FROM server_chat_usage ORDER BY account_id").fetchall() == [
            ("lumi-group", 2), ("node", 2),
        ]
        assert storage.execute("SELECT state,message_id FROM server_lumi_group_deliveries WHERE update_id=703").fetchone() == ("delivered", 9703)
        assert storage.execute("SELECT state,message_id FROM server_lumi_group_deliveries WHERE update_id=705").fetchone() == ("rejected", 0)


def check_feedback_and_unlink(port, database, owner, owner_auth, other, other_auth, calls):
    with closing(sqlite3.connect(database)) as storage, storage:
        storage.execute("INSERT INTO server_app_feedback(account_id,id,app_id,title,message,context) VALUES(?,?,?,?,?,?)",
                        (other, "f" * 64, "inbe", "Lag", "Slow view", "private test"))
    assert poll(port, other_auth)["update"] == NO_UPDATE
    update = poll(port, owner_auth)["update"]
    assert update["id"] < 0 and update["kind"] == "feedback" and "can_reply" not in update
    assert json.loads(update["text"]) == {"title": "Lag", "message": "Slow view", "account_id": other, "report_id": "f" * 64}
    assert ack(port, owner_auth, update["id"]) is True
    assert poll(port, owner_auth)["update"] == NO_UPDATE
    with closing(sqlite3.connect(database)) as storage, storage:
        assert storage.execute("SELECT owner_delivered FROM server_app_feedback WHERE id=?", ("f" * 64,)).fetchone() == (1,)
        storage.execute("INSERT INTO server_app_feedback(account_id,id,app_id,title,message,context) VALUES(?,?,?,?,?,?)",
                        (other, "e" * 64, "inbe", "Delete", "Erase this copy", ""))
    assert poll(port, owner_auth)["update"]["kind"] == "feedback"
    with closing(sqlite3.connect(database)) as storage, storage:
        assert storage.execute("DELETE FROM server_app_feedback WHERE id=?", ("e" * 64,)).rowcount == 1
        for bot_id in (0, 345):
            storage.execute("INSERT INTO server_authorization_requests(request_id,account_id,app_id,node_id,audience,expires_at,bot_id,scopes_json,claim_nonce) VALUES(?,?,'inbe','node','audience',?,?,'[]','request-nonce')",
                            (str(bot_id), owner, int(time.time()) + 60, bot_id))
            storage.execute("INSERT INTO server_authorization_grants(grant_id,account_id,request_id,app_id,node_id,audience,not_before,expires_at,nonce,grant_json) VALUES(?,?,?,'inbe','node','audience',0,?,?,'{}')",
                            (str(bot_id), owner, str(bot_id), int(time.time()) + 60, str(bot_id)))
    assert poll(port, owner_auth)["update"] == NO_UPDATE
    outstanding = issue(port, owner_auth)
    assert request(port, "DELETE", BASE + "link", headers=owner_auth) is True
    assert not poll(port, owner_auth)["linked"] and poll(port, other_auth)["linked"]
    assert webhook(port, message(900, "/start " + outstanding)) == {"link_status": "expired"}
    assert ack(port, owner_auth, 202, reply="after unlink", expected=409) == {"error": "no matching Telegram message claim"}
    before = len(calls)
    body = json.dumps({"id": "old-client", "text": "é" * 2048, "draft": "a" * 32}).encode().ljust(16384)
    gzip_headers = owner_auth | {"Content-Encoding": "gzip"}
    assert request(port, "POST", BASE + "messages", headers=gzip_headers, body=gzip.compress(body)) is True
    assert request(port, "POST", BASE + "messages", headers=gzip_headers, body=gzip.compress(body + b" "), expected=400) == {"error": "invalid message"}
    raw = json.dumps(message(901, "", sender=0)).encode().ljust(32768)
    assert webhook(port, raw) is True
    assert webhook(port, raw + b" ", expected=400) == {"error": "invalid update"}
    assert len(calls) == before
    with closing(sqlite3.connect(database)) as storage, storage:
        assert storage.execute("SELECT request_id,status FROM server_authorization_requests ORDER BY request_id").fetchall() == [
            ("0", "pending"), ("345", "cancelled"),
        ]
        assert storage.execute("SELECT revoked_at>0 FROM server_authorization_grants ORDER BY grant_id").fetchall() == [(0,), (1,)]
        storage.execute("UPDATE server_lumi_updates SET done_at=? WHERE id=202", (int(time.time()) - 86410,))


def main():
    binary, oracle = map(str, map(Path.resolve, map(Path, sys.argv[1:3])))
    if sys.argv[3:] == ["--server"]:
        # The shared suite uses the production endpoint with the bot disabled.
        with TemporaryDirectory(prefix="lumi-telegram-disabled-", dir=Path(binary).parent) as scratch:
            with node(binary, Path(scratch)) as (port, _):
                check_disabled(port, oracle, [])
        print("Native Lumi Telegram HTTP: disabled routes and offline history passed.")
        return
    if sys.argv[3:]:
        raise SystemExit("usage: check_lumi_telegram_http.py SERVER ORACLE [--server]")
    with TemporaryDirectory(prefix="lumi-telegram-", dir=Path(binary).parent) as scratch, fake_provider() as (provider, calls):
        directory = Path(scratch)
        with node(binary, directory) as (port, database):
            owner, owner_auth, other, other_auth = check_disabled(port, oracle, calls)
        legacy_tables(database, owner)
        settings = {"MESH_LUMI_BOT_TOKEN": BOT, "MESH_LUMI_WEBHOOK_SECRET": SECRET, "MESH_LUMI_OWNER_ID": owner,
                    "MESH_LUMI_GROUP_ID": str(GROUP), "MESH_LUMI_CANVAS_URL": CANVAS,
                    "MESH_LUMI_FIXTURE_TELEGRAM_PORT": str(provider), "MESH_LUMI_FIXTURE_MODEL_PORT": str(provider)}
        with node(binary, directory, **settings) as (port, database):
            expired = check_link(port, database, owner_auth, other_auth, calls)
            check_private(port, database, owner_auth, other_auth, calls)
            check_groups(port, database, calls)
            check_feedback_and_unlink(port, database, owner, owner_auth, other, other_auth, calls)
        with node(binary, directory) as (_, database), closing(sqlite3.connect(database)) as storage:
            assert storage.execute("SELECT id FROM server_lumi_updates WHERE done=1 ORDER BY id").fetchall() == [(204,), (205,)]
            assert storage.execute("SELECT 1 FROM server_lumi_link_codes WHERE code=?", (expired,)).fetchone() is None
            assert storage.execute("SELECT telegram_id FROM server_lumi_telegram WHERE account_id=?", (other,)).fetchone() == (5678,)
    print("Native Lumi Telegram HTTP: link races and expiry, private queue and delivery, group scope, feedback isolation and retention passed.")


if __name__ == "__main__":
    main()
