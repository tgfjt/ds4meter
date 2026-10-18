#!/usr/bin/env python3
"""Token-metering proxy for ds4-server.

It forwards every request to the upstream unchanged and records usage from
/v1/chat/completions, /v1/completions, /v1/responses and /v1/messages
responses, streaming included, into SQLite. View the records with tui.py.

    python3 proxy.py --listen 8002 --upstream http://127.0.0.1:8001
"""
import argparse
import http.client
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
TRACKED = ("/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT,
    client TEXT,
    stream INTEGER NOT NULL,
    status INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    duration_ms REAL,
    ttft_ms REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
"""


def _int(v):
    return v if isinstance(v, int) else None


def _openai_chat_usage(u):
    return {
        "input_tokens": _int(u.get("prompt_tokens")),
        "output_tokens": _int(u.get("completion_tokens")),
        "cached_tokens": _int((u.get("prompt_tokens_details") or {}).get("cached_tokens")),
        "reasoning_tokens": _int((u.get("completion_tokens_details") or {}).get("reasoning_tokens")),
    }


def _responses_or_anthropic_usage(u):
    out = {}
    if "input_tokens" in u:
        inp = _int(u.get("input_tokens"))
        # Anthropic input_tokens excludes cached tokens, so add them back
        extra = sum(_int(u.get(k)) or 0 for k in ("cache_read_input_tokens", "cache_creation_input_tokens"))
        out["input_tokens"] = inp + extra if inp is not None else None
    if "output_tokens" in u:
        out["output_tokens"] = _int(u.get("output_tokens"))
    cached = _int((u.get("input_tokens_details") or {}).get("cached_tokens"))
    out["cached_tokens"] = cached if cached is not None else _int(u.get("cache_read_input_tokens"))
    out["reasoning_tokens"] = _int((u.get("output_tokens_details") or {}).get("reasoning_tokens"))
    return out


def usage_from_obj(u):
    if not isinstance(u, dict):
        return {}
    is_chat = "prompt_tokens" in u or "completion_tokens" in u
    out = _openai_chat_usage(u) if is_chat else _responses_or_anthropic_usage(u)
    return {k: v for k, v in out.items() if v is not None}


def usage_from_json_body(data):
    if not isinstance(data, dict):
        return {}, None
    if isinstance(data.get("response"), dict):
        data = data["response"]
    return usage_from_obj(data.get("usage")), data.get("model")


class StreamUsage:
    def __init__(self):
        self.usage = {}
        self.model = None

    def feed(self, data):
        typ = data.get("type")
        if typ == "message_start":
            msg = data.get("message") or {}
            self.model = msg.get("model") or self.model
            self.usage.update(usage_from_obj(msg.get("usage")))
            return
        if typ == "message_delta":  # Anthropic: output_tokens is cumulative
            self.usage.update(usage_from_obj(data.get("usage")))
            return
        u, model = usage_from_json_body(data)
        self.usage.update(u)
        self.model = model or self.model


def is_injected_usage_chunk(data):
    return bool(data.get("usage")) and not data.get("choices")


def client_name(ua):
    if not ua:
        return None
    return ua.split()[0].split("/")[0]


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def insert(self, row):
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        with self.lock, self._conn() as c:
            c.execute(f"INSERT INTO requests ({cols}) VALUES ({qs})", list(row.values()))

    DAILY_SQL = """SELECT date(ts,'unixepoch','localtime') AS day, COUNT(*) AS requests,
                          SUM(COALESCE(input_tokens,0)) AS input_tokens,
                          SUM(COALESCE(output_tokens,0)) AS output_tokens,
                          SUM(COALESCE(cached_tokens,0)) AS cached_tokens,
                          SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens
                   FROM requests WHERE ts >= ? GROUP BY day ORDER BY day"""

    def hourly(self, since):
        with self._conn() as c:
            return [dict(r) for r in c.execute(self.DAILY_SQL.replace(
                "date(ts,'unixepoch','localtime') AS day", "strftime('%H',ts,'unixepoch','localtime') AS hour"
            ).replace("GROUP BY day ORDER BY day", "GROUP BY hour ORDER BY hour"), (since,))]

    def daily(self, days):
        with self._conn() as c:
            return [dict(r) for r in c.execute(self.DAILY_SQL, (time.time() - days * 86400,))]

    def stats(self, days):
        since = time.time() - days * 86400
        with self._conn() as c:
            daily = c.execute(self.DAILY_SQL, (since,)).fetchall()

            def breakdown(col):
                return c.execute(
                    f"""SELECT COALESCE({col},'(unknown)') AS name, COUNT(*) AS requests,
                               SUM(COALESCE(input_tokens,0)) AS input_tokens,
                               SUM(COALESCE(output_tokens,0)) AS output_tokens
                        FROM requests WHERE ts >= ? GROUP BY name
                        ORDER BY input_tokens + output_tokens DESC LIMIT 20""", (since,)).fetchall()

            decode = c.execute(
                """SELECT SUM(output_tokens) AS toks, SUM(duration_ms - ttft_ms) AS ms
                   FROM requests WHERE ts >= ? AND stream = 1 AND output_tokens > 0
                     AND ttft_ms IS NOT NULL AND duration_ms > ttft_ms""", (since,)).fetchone()
            recent = c.execute(
                """SELECT ts, endpoint, model, client, stream, status, input_tokens, output_tokens,
                          cached_tokens, reasoning_tokens, duration_ms, ttft_ms, error
                   FROM requests ORDER BY ts DESC LIMIT 500""").fetchall()
        return {
            "daily": [dict(r) for r in daily],
            "by_model": [dict(r) for r in breakdown("model")],
            "by_client": [dict(r) for r in breakdown("client")],
            "tokens_per_sec": (decode["toks"] / (decode["ms"] / 1000)) if decode["ms"] else None,
            "recent": [dict(r) for r in recent],
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None
    store = None

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self._proxy()

    do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST

    def _proxy(self):
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        tracked = self.command == "POST" and path in TRACKED

        req_json, injected = None, False
        if tracked and body:
            try:
                req_json = json.loads(body)
            except ValueError:
                pass
        stream = bool(req_json and req_json.get("stream"))
        # Chat/Completions streams only report usage when asked, so request it
        if stream and path in ("/v1/chat/completions", "/v1/completions"):
            opts = req_json.get("stream_options") or {}
            if not opts.get("include_usage"):
                req_json["stream_options"] = {**opts, "include_usage": True}
                body = json.dumps(req_json).encode()
                injected = True

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        if body is not None:
            headers["Content-Length"] = str(len(body))

        scheme, host, port = self.upstream
        conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=3600)
        t0 = time.time()
        rec = {
            "ts": t0, "endpoint": path,
            "model": (req_json or {}).get("model"),
            "client": client_name(self.headers.get("User-Agent")),
            "stream": int(stream), "status": None,
        }
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            if tracked:
                self.store.insert({**rec, "status": 502, "error": str(e), "duration_ms": (time.time() - t0) * 1000})
            err = json.dumps({"error": f"upstream: {e}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        rec["status"] = resp.status
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)

        is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "")
        try:
            if is_sse:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                usage, model, ttft = self._pipe_sse(resp, injected, t0)
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                usage, model, ttft = {}, None, None
                if tracked:
                    try:
                        usage, model = usage_from_json_body(json.loads(data))
                    except ValueError:
                        pass
                    if resp.status >= 400:
                        rec["error"] = data[:500].decode("utf-8", "replace")
        except (BrokenPipeError, ConnectionResetError) as e:
            usage, model, ttft = {}, None, None
            rec["error"] = f"client disconnected: {e}"
        finally:
            conn.close()

        if tracked:
            rec.update(usage)
            rec["model"] = model or rec["model"]
            rec["duration_ms"] = (time.time() - t0) * 1000
            rec["ttft_ms"] = ttft
            self.store.insert(rec)

    def _pipe_sse(self, resp, injected, t0):
        acc = StreamUsage()
        ttft = None
        skip_blank = False
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload and payload != b"[DONE]":
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        if ttft is None:
                            ttft = (time.time() - t0) * 1000
                        acc.feed(data)
                        if injected and is_injected_usage_chunk(data):
                            skip_blank = True
                            continue
            elif skip_blank and line.strip() == b"":
                skip_blank = False
                continue
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.flush()
        return acc.usage, acc.model, ttft


def make_server(listen, upstream, db):
    up = urlsplit(upstream)
    Handler.upstream = (up.scheme, up.hostname, up.port or (443 if up.scheme == "https" else 80))
    Handler.store = Store(db)

    host, _, port = listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    srv.daemon_threads = True
    print(f"proxy  http://{host or '127.0.0.1'}:{port}  ->  {upstream}   db: {db}", flush=True)
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1:8002", help="host:port or port (default 127.0.0.1:8002)")
    ap.add_argument("--upstream", default="http://127.0.0.1:8001", help="ds4-server base URL")
    ap.add_argument("--db", default=str(HERE / "usage.db"))
    args = ap.parse_args()
    srv = make_server(args.listen, args.upstream, args.db)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
