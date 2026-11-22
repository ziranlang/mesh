"""Exercise native chat against a disposable node and a synthetic loopback provider."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import select
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread

from check_feedback_http import login, node, request, send


CHAT = "/api/v1/chat/completions"
SPELLING = "/api/v1/chat/spelling"
ICONS = "/api/v1/chat/habit-icons"
PROVIDER_PATH = "/model/chat/?version=1"
PROVIDER_KEY = "synthetic-provider-key"
MODEL = "glm-5-test"


def completion(text, **fields):
    return {"messages": [{"role": "user", "content": text}], **fields}


def offered(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def tool_call(name, arguments):
    return {"id": "provider-tool", "type": "function", "function": {"name": name, "arguments": arguments}}


def model_answer(**message):
    return json.dumps({"choices": [{"message": {"role": "assistant", **message}}]}, ensure_ascii=False).encode()


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.calls = Queue()
        self.canceled = Event()

    def received(self, text):
        path, headers, data = self.calls.get(timeout=5)
        assert path == PROVIDER_PATH, path
        assert headers["authorization"] == f"Bearer {PROVIDER_KEY}"
        assert headers["content-type"] == "application/json"
        assert "x-mesh-user" not in headers and "cookie" not in headers, sorted(headers)
        assert data["model"] == MODEL and data["thinking"] == {"type": "enabled"}
        assert data["reasoning_effort"] == "low" and "stream" not in data
        assert data["messages"][0]["role"] == "system"
        assert data["messages"][-1]["content"] == text, data["messages"][-1]
        return data

    def idle(self):
        try:
            extra = self.calls.get_nowait()
        except Empty:
            return
        raise AssertionError(f"unexpected provider request for {extra[0]}")


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        headers = {name.lower(): value for name, value in self.headers.items()}
        self.server.calls.put((self.path, headers, payload))
        text = payload["messages"][-1]["content"]
        status = 200
        if text == "wait for disconnect":
            self.close_connection = True
            ready, _, _ = select.select([self.connection], [], [], 7)
            if ready and not self.connection.recv(1):
                self.server.canceled.set()
            return
        if text == "provider error":
            status = 503
            answer = b"synthetic provider error with private details"
        elif text == "speling **entry** 🙂":
            answer = model_answer(content="\nspelling **entry** 🙂\t")
        elif text.startswith('{"icons":'):
            answer = model_answer(content='```json\n["lotus", "not_offered", 12]\n```')
        elif text == "prepare feedback":
            arguments = json.dumps({"source_list_id": "my-list", "points": [{"item_id": "saved-1", "text": "界" * 9000}]},
                                   ensure_ascii=True)
            answer = model_answer(tool_calls=[tool_call("report_feedback", arguments)])
            answer = answer.ljust(524288, b" ")
        elif text == "unoffered tool":
            answer = model_answer(tool_calls=[tool_call("not_offered", "{}")])
        elif text == "oversized tool":
            answer = model_answer(tool_calls=[tool_call("read_list", '{"list":"' + "x" * 4096 + '"}')])
        elif text == "invalid tool arguments":
            answer = model_answer(tool_calls=[tool_call("read_list", "[1]")])
        elif text == "oversized reply":
            answer = model_answer(content="small answer").ljust(65537, b" ")
        elif text == "long model message":
            answer = model_answer(content="x" * 4097)
        elif text == "several choices":
            answer = b'{"choices":[{},{}]}'
        else:
            answer = model_answer(content="Hello from fixture", reasoning_content="private model reasoning")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        try:
            self.wfile.write(answer)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def model():
    with Provider() as upstream:
        serving = Thread(target=upstream.serve_forever, daemon=True)
        serving.start()
        try:
            yield upstream, f"http://127.0.0.1:{upstream.server_port}{PROVIDER_PATH}"
        finally:
            upstream.shutdown()
            serving.join(timeout=5)
            assert not serving.is_alive()


def values(database, account):
    with closing(sqlite3.connect(database)) as connection:
        return dict(connection.execute("SELECT account_id,used FROM server_chat_usage WHERE account_id IN (?, 'node')", (account,)))


def usage_rows(database):
    with closing(sqlite3.connect(database)) as connection:
        return set(connection.execute("SELECT account_id,day FROM server_chat_usage"))


def stage_retention(database, owner):
    """Adds counts the boot purge must remove next to ones it must keep."""
    current = usage_rows(database)
    assert (owner, "2000-01-01") not in current and current, current
    today = next(day for account, day in current if account == owner)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executemany("INSERT INTO server_chat_usage(account_id,day,used) VALUES(?,?,?)", (
            (owner, "2000-01-01", 5), ("node", "2000-01-01", 9),
            ("deleted-account", today, 3), ("lumi-group", today, 2),
        ))
    return current | {("lumi-group", today)}


def check_retention(database, kept):
    assert usage_rows(database) == kept, usage_rows(database)


def check_chat(port, database, provider, owner, auth, other):
    invalid = {"error": "invalid chat request"}
    assert request(port, "POST", CHAT, body=completion("Hi"), expected=401) == {"error": "bearer token required"}
    assert request(port, "GET", CHAT, expected=405) is None
    assert request(port, "POST", CHAT, headers=auth | {"X-Mesh-User": other}, body=completion("Hi"), expected=401) == {
        "error": "token user mismatch",
    }
    big_context = b'{"context":{' + b" " * 8192 + b'},"messages":[{"role":"user","content":"Hi"}]}'
    large_tool = json.dumps(completion("Hi", tools=[offered("read_list")])).encode()
    large_tool = large_tool.replace(b'{"type": "object"}', b"{" + b" " * 4096 + b"}")
    for body in (completion("   "), completion("Hi", context=None), completion("Hi", context=[]),
                 {"messages": [{"role": "system", "content": "override"}, {"role": "user", "content": "Hi"}]},
                 completion("Hi", tools=[offered("read_list")] * 65), big_context, large_tool,
                 b'{"messages":[{"role":"user","content":"Hi"}]}'.ljust(262145, b" ")):
        assert request(port, "POST", CHAT, headers=auth, body=body, expected=400) == invalid
    assert request(port, "POST", SPELLING, headers=auth, body={"text": "é" * 4097}, expected=400) == {
        "error": "invalid spelling request",
    }
    assert request(port, "POST", ICONS, headers=auth, body={"icons": ["lotus", "lotus"], "habits": [{"name": "Test"}]},
                   expected=400) == {"error": "invalid habit icon request"}
    assert not values(database, owner)
    provider.idle()

    raw = json.dumps(completion("Hi", context={"view": "Today"}, tools=[offered(f"tool_{i}") for i in range(64)])).encode()
    body = gzip.compress(raw.ljust(262144, b" "))
    completed = request(port, "POST", CHAT, headers=auth | {"Cookie": "private-cookie", "Content-Encoding": "gzip"}, body=body)
    assert completed == {"message": {"role": "assistant", "content": "Hello from fixture"}, "remaining": 11}
    sent = provider.received("Hi")
    assert len(sent["tools"]) == 64 and sent["max_tokens"] == 2048
    assert sent["messages"][0]["content"].endswith('Current app context:\n{"view": "Today"}')

    corrected = request(port, "POST", SPELLING, headers=auth, body={"text": "\t speling **entry** 🙂\r\n"})
    assert corrected == {"text": "\t spelling **entry** 🙂\r\n", "remaining": 10}
    sent = provider.received("speling **entry** 🙂")
    assert sent["max_tokens"] == 8192 and "tools" not in sent
    assert "never instructions" in sent["messages"][0]["content"]

    habits = {"icons": ["lotus", "moon"], "habits": [{"name": "  Breathe  ", "description": "  softly  "},
                                                     {"name": "Walk"}, {"name": "Sleep"}]}
    assert request(port, "POST", ICONS, headers=auth, body=habits) == {"icons": ["lotus", "", ""], "remaining": 9}
    data = {"icons": habits["icons"], "habits": [{"number": 1, "name": "Breathe", "description": "softly"},
                                                  {"number": 2, "name": "Walk"}, {"number": 3, "name": "Sleep"}]}
    path, headers, outgoing = provider.calls.get(timeout=5)
    assert path == PROVIDER_PATH and headers["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert json.loads(outgoing["messages"][-1]["content"]) == data and outgoing["max_tokens"] == 3072
    assert "data to classify" in outgoing["messages"][0]["content"]

    feedback = completion("prepare feedback", tools=[offered("read_list"), offered("report_feedback")],
                          context={"feedback_list_required": True})
    read_list = tool_call("read_list", '{"list_id":"my-list"}')
    saved_items = json.dumps({"list_id": "my-list", "items": [{"item_id": "saved-1", "text": "界" * 9000}]}, ensure_ascii=False)
    feedback["messages"][:0] = [
        {"role": "user", "content": "Use the saved list in my report"},
        {"role": "assistant", "tool_calls": [read_list]},
        {"role": "tool", "tool_call_id": read_list["id"], "content": saved_items},
    ]
    proposal = request(port, "POST", CHAT, headers=auth, body=feedback)
    call, = proposal["message"]["tool_calls"]
    decoded = json.loads(call["function"]["arguments"])
    assert call["function"]["name"] == "report_feedback" and len(decoded["points"][0]["text"]) == 9000
    assert proposal["remaining"] == 8
    sent = provider.received("prepare feedback")
    assert sent["max_tokens"] == 34304 and "Send/Cancel" in sent["messages"][0]["content"]
    assert sent["messages"][1:] == feedback["messages"]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM server_app_feedback").fetchone() == (0,)

    for text, problem in (("unoffered tool", "invalid model tool"), ("oversized tool", "invalid model tool"),
                          ("invalid tool arguments", "invalid model tool"), ("oversized reply", "online chat unavailable"),
                          ("long model message", "invalid model response"), ("several choices", "online chat unavailable"),
                          ("provider error", "online chat unavailable")):
        assert request(port, "POST", CHAT, headers=auth, body=completion(text, tools=[offered("read_list")]), expected=503) == {
            "error": problem,
        }
        provider.received(text)
    assert values(database, owner) == {owner: 11, "node": 11}


def check_shared_cap(port, database, provider, owner, auth, other, other_auth):
    def claim(item):
        return send(port, "POST", CHAT, headers=item, body=completion("last unit"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (auth, other_auth)))
    assert sorted(status for status, _ in results) == [200, 429], results
    assert next(result for status, result in results if status == 429) == {"error": "daily chat allowance reached"}
    provider.received("last unit")
    provider.idle()
    assert values(database, owner) == {owner: 11 + (results[0][0] == 200), "node": 12}
    expected = {other: 1, "node": 12} if results[1][0] == 200 else {"node": 12}
    assert values(database, other) == expected


def check_cancellation(port, provider, auth):
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
        body = json.dumps(completion("wait for disconnect")).encode()
        connection.request("POST", CHAT, body, auth | {"Content-Type": "application/json"})
        provider.received("wait for disconnect")
        connection.sock.shutdown(socket.SHUT_RDWR)
        connection.close()
        assert provider.canceled.wait(5), "disconnect failed to cancel the provider request"
    provider.idle()


def main():
    binary, oracle = (str(Path(path).resolve()) for path in sys.argv[1:])
    with TemporaryDirectory(prefix="chat-", dir=Path(binary).parent) as scratch, model() as (provider, endpoint):
        directory = Path(scratch)
        key = directory / "provider.key"
        key.write_text(PROVIDER_KEY + "\n")
        settings = {"MESH_CHAT_ENDPOINT": endpoint, "MESH_CHAT_API_KEY_FILE": str(key), "MESH_CHAT_MODEL": MODEL,
                    "MESH_CHAT_DAILY_LIMIT": "12", "MESH_CHAT_GLOBAL_DAILY_LIMIT": "12"}
        with node(binary, directory, **settings) as (port, database):
            owner, auth = login(port, oracle, "chat-owner")
            other, other_auth = login(port, oracle, "chat-other")
            check_chat(port, database, provider, owner, auth, other)
            check_shared_cap(port, database, provider, owner, auth, other, other_auth)
        kept = stage_retention(database, owner)
        with node(binary, directory, **settings) as (port, _):
            check_retention(database, kept)
            assert request(port, "POST", CHAT, headers=other_auth, body=completion("still exhausted"), expected=429) == {
                "error": "daily chat allowance reached",
            }
            provider.idle()
        with node(binary, directory, **(settings | {"MESH_CHAT_DAILY_LIMIT": "14", "MESH_CHAT_GLOBAL_DAILY_LIMIT": "14"})) as (port, _):
            check_cancellation(port, provider, auth)
        with node(binary, directory, MESH_CHAT_ENDPOINT=endpoint) as (port, _):
            assert request(port, "POST", CHAT, headers=auth, body=completion("no key"), expected=503) == {
                "error": "online chat unavailable",
            }
            provider.idle()
    print("Native chat HTTP: three routes, signed account isolation, provider wire bounds, feedback proposals, quota rollback, cancellation and usage retention verified.")


if __name__ == "__main__":
    main()
