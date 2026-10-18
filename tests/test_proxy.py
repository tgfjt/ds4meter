import io
import json
import socket
import sqlite3
import threading
import unittest
from unittest import mock

from support import check_golden, frozen_clock, proxy, tmpdir

FORMATS = ("/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages")


class FakeResponse:
    def __init__(self, status, ctype, body):
        self.status = status
        self._headers = [("Content-Type", ctype)]
        self._body = io.BytesIO(body)

    def getheaders(self):
        return self._headers

    def getheader(self, name, default=None):
        return dict(self._headers).get(name, default)

    def read(self):
        return self._body.read()

    def readline(self):
        return self._body.readline()


def sse(events):
    out = b""
    for event, data in events:
        if event:
            out += f"event: {event}\n".encode()
        out += b"data: " + (data if isinstance(data, bytes) else json.dumps(data).encode()) + b"\n\n"
    return out


def upstream_reply(path, req):
    if path == "/v1/models":
        return FakeResponse(200, "application/json", b'{"data":[]}')
    if path == "/v1/fail" or req.get("fail"):
        return FakeResponse(500, "application/json", b'{"error":"boom"}')
    if not req.get("stream"):
        body = {
            "/v1/messages": {"model": "dsv4", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}},
            "/v1/responses": {"model": "dsv4", "usage": {
                "input_tokens": 100, "input_tokens_details": {"cached_tokens": 50},
                "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 5}}},
        }.get(path, {"model": "dsv4", "choices": [{}], "usage": {
            "prompt_tokens": 11, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2}}})
        return FakeResponse(200, "application/json", json.dumps(body).encode())
    if path == "/v1/messages":
        events = [
            ("message_start", {"type": "message_start", "message": {"model": "dsv4", "usage": {
                "input_tokens": 5, "cache_read_input_tokens": 20, "output_tokens": 1}}}),
            ("content_block_delta", {"type": "content_block_delta"}),
            ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 42}}),
        ]
    elif path == "/v1/responses":
        events = [
            (None, {"type": "response.output_text.delta"}),
            (None, {"type": "response.completed", "response": {"model": "dsv4", "usage": {"input_tokens": 30, "output_tokens": 9}}}),
        ]
    else:
        events = [(None, {"choices": [{"delta": {}}]})]
        if (req.get("stream_options") or {}).get("include_usage"):
            events.append((None, {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6}}))
        events.append((None, b"[DONE]"))
    return FakeResponse(200, "text/event-stream", sse(events))


class FakeConnection:
    sent = []
    refuse = False

    def __init__(self, *args, **kwargs):
        pass

    def request(self, method, path, body=None, headers=None):
        if self.refuse:
            raise ConnectionRefusedError(61, "Connection refused")
        self.path, self.req = path, json.loads(body) if body else {}
        FakeConnection.sent.append({"method": method, "path": path, "body": self.req})

    def getresponse(self):
        return upstream_reply(self.path, self.req)

    def close(self):
        pass


def call(method, path, body=None, ua="claude-cli/2.1.3 (external, cli)"):
    client, server = socket.socketpair()
    data = json.dumps(body).encode() if body is not None else b""
    client.sendall(f"{method} {path} HTTP/1.1\r\nHost: x\r\nUser-Agent: {ua}\r\n"
                   f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
    t = threading.Thread(target=lambda: (proxy.Handler(server, ("x", 0), None), server.close()))
    t.start()
    t.join(5)
    raw = b""
    while chunk := client.recv(65536):
        raw += chunk
    client.close()
    head, _, rest = raw.decode().partition("\r\n\r\n")
    status = head.split("\r\n")[0]
    headers = sorted(h for h in head.split("\r\n")[1:] if not h.startswith(("Date:", "Server:")))
    return {"status": status, "headers": headers, "body": rest}


def run_traffic():
    responses = {}
    FakeConnection.sent, FakeConnection.refuse = [], False
    with tmpdir() as d, frozen_clock(), mock.patch.object(proxy.http.client, "HTTPConnection", FakeConnection):
        proxy.Handler.upstream = ("http", "x", 1)
        proxy.Handler.store = proxy.Store(f"{d}/usage.db")
        for path in FORMATS:
            for stream in (False, True):
                responses[f"{path} stream={stream}"] = call("POST", path, {"model": "deepseek-v4-flash", "stream": stream},
                                                            ua="codex_cli_rs/0.5 (mac)")
        responses["chat client asked for usage"] = call("POST", "/v1/chat/completions",
                                                        {"model": "m", "stream": True, "stream_options": {"include_usage": True}})
        responses["passthrough GET"] = call("GET", "/v1/models")
        responses["upstream 500"] = call("POST", "/v1/fail", {"model": "m"})
        FakeConnection.refuse = True
        responses["upstream down"] = call("POST", "/v1/messages", {"model": "m"})
        rows = [list(r) for r in sqlite3.connect(f"{d}/usage.db").execute(
            "SELECT endpoint, model, client, stream, status, input_tokens, output_tokens, cached_tokens,"
            " reasoning_tokens, ttft_ms IS NOT NULL, error FROM requests ORDER BY id")]
    return {"responses": responses, "sent": FakeConnection.sent, "rows": rows}


def disconnect_midway(path, body):
    client, server = socket.socketpair()
    data = json.dumps(body).encode()
    client.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
    client.close()
    proxy.Handler(server, ("x", 0), None)
    server.close()


class Outcome(unittest.TestCase):
    def outcomes(self):
        FakeConnection.sent, FakeConnection.refuse = [], False
        with tmpdir() as d, frozen_clock(), mock.patch.object(proxy.http.client, "HTTPConnection", FakeConnection):
            proxy.Handler.upstream = ("http", "x", 1)
            proxy.Handler.store = proxy.Store(f"{d}/usage.db")
            call("POST", "/v1/messages", {"model": "m"})
            call("POST", "/v1/messages", {"model": "m", "fail": True})
            disconnect_midway("/v1/chat/completions", {"model": "m", "stream": True})
            FakeConnection.refuse = True
            call("POST", "/v1/messages", {"model": "m"})
            return sorted(proxy.request_outcome(r) for r in proxy.Handler.store.stats(1)["recent"])

    def test_client_disconnect_is_canceled_not_failed(self):
        self.assertEqual(self.outcomes(), ["canceled", "failed", "failed", "ok"])


class ProxyTraffic(unittest.TestCase):
    def test_traffic_matches_golden(self):
        out = run_traffic()
        check_golden(self, "proxy", {
            **{f"response {k}": v for k, v in out["responses"].items()},
            "sent upstream": out["sent"],
            "recorded rows": out["rows"],
        })

    def test_injected_usage_chunk_never_reaches_client(self):
        body = run_traffic()["responses"]["/v1/chat/completions stream=True"]["body"]
        self.assertNotIn('"usage"', body)


if __name__ == "__main__":
    unittest.main()
