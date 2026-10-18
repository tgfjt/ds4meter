#!/usr/bin/env python3
"""ds4-server をいつものコマンドのまま起動し、その前にプロキシを透過的に挟む。

    python3 serve.py ./ds4-server --ctx 100000 --host 0.0.0.0 --port 8001

ds4-server に渡した --host/--port はプロキシが引き取り、ds4-server 本体は
127.0.0.1:(port + 10000) に下げて起動する。クライアントは今まで通り :8001 に
つなぐだけで記録される。Ctrl-C で両方止まる。
"""
import errno
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import proxy

HERE = Path(__file__).resolve().parent
DS4_DEFAULT_PORT = 8000
BACKEND_OFFSET = 10000


def split_listen_args(argv):
    host, port, rest = None, None, []
    it = iter(argv)
    for a in it:
        if a in ("--host", "--port"):
            v = next(it, None)
            if a == "--host":
                host = v
            else:
                port = int(v)
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        else:
            rest.append(a)
    return host, port, rest


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    host, port, rest = split_listen_args(argv)
    host = host or "127.0.0.1"
    port = port or DS4_DEFAULT_PORT
    backend_port = port + BACKEND_OFFSET
    db = os.environ.get("USAGE_DB", str(HERE / "usage.db"))

    try:
        srv = proxy.make_server(f"{host}:{port}", f"http://127.0.0.1:{backend_port}", db)
    except OSError as e:
        hint = "\n(既存の ds4-server がまだ動いていませんか？)" if e.errno == errno.EADDRINUSE else ""
        sys.exit(f"{host}:{port} を開けません: {e}{hint}")

    cmd = rest + ["--host", "127.0.0.1", "--port", str(backend_port)]
    print("backend  " + " ".join(cmd), flush=True)
    child = subprocess.Popen(cmd)

    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop(*_):
        if child.poll() is None:
            child.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, stop)
    try:
        code = child.wait()
    except KeyboardInterrupt:
        stop()
        code = child.wait()
    srv.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()
