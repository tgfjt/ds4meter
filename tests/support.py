import contextlib
import datetime as dt
import json
import os
import random
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
sys.path.insert(0, str(REPO))

os.environ["TZ"] = "Asia/Tokyo"
time.tzset()

import proxy  # noqa: E402
import tui  # noqa: E402

TODAY = dt.date(2026, 10, 18)
NOW = time.mktime(TODAY.timetuple()) + 12 * 3600


class _FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return TODAY


@contextlib.contextmanager
def frozen_clock():
    shim = types.SimpleNamespace(date=_FrozenDate, timedelta=dt.timedelta)
    with mock.patch("time.time", return_value=NOW), mock.patch.object(tui, "dt", shim):
        yield


def _seed_rows():
    rng = random.Random(1)
    rows = []
    for d in range(400):
        for _ in range(rng.randint(0, 25) if d % 6 else 0):
            inp, out, dur = rng.randint(2000, 60000), rng.randint(100, 4000), rng.uniform(3000, 60000)
            rows.append({
                "ts": NOW - d * 86400 - rng.uniform(0, 30000),
                "endpoint": rng.choice(["/v1/messages", "/v1/responses", "/v1/chat/completions"]),
                "model": rng.choice(["deepseek-v4-flash", "qwen3.8-flash-q4-long-name"]),
                "client": rng.choice(["claude-cli", "codex_cli_rs", "opencode"]),
                "stream": rng.choice([0, 1]), "status": rng.choice([200] * 20 + [500]),
                "input_tokens": inp, "output_tokens": out, "cached_tokens": int(inp * rng.random()),
                "reasoning_tokens": out // 3, "duration_ms": dur, "ttft_ms": dur * 0.3, "error": None,
            })
    return rows


DATASETS = {
    "empty": lambda: [],
    "one": lambda: [{
        "ts": NOW, "endpoint": "/v1/chat/completions", "model": "qwen3.8-flash-q4", "client": "lorca-agent",
        "stream": 1, "status": 200, "input_tokens": 15341, "output_tokens": 254, "cached_tokens": 0,
        "duration_ms": 8000, "ttft_ms": 2740,
    }],
    "levels": lambda: [{
        "ts": NOW - i * 86400, "endpoint": "/v1/messages", "model": "m", "client": "c", "stream": 0,
        "status": 200, "input_tokens": v, "output_tokens": 0,
    } for i, v in enumerate([50_000, 120_000, 600_000, 2_500_000, 0, 90_000]) if v],
    "seed": _seed_rows,
}


def make_store(dataset, tmpdir):
    store = proxy.Store(os.path.join(tmpdir, f"{dataset}.db"))
    for row in DATASETS[dataset]():
        store.insert(row)
    return store


def check_golden(case, name, actual):
    """UPDATE_GOLDEN=1 で記録し直す。差分があれば最初に食い違ったキーで失敗させる。"""
    path = GOLDEN / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        return
    expected = json.loads(path.read_text())
    case.assertEqual(sorted(expected), sorted(actual), f"{name}: case set changed")
    for key in sorted(expected):
        case.assertEqual(expected[key], actual[key], f"{name}: {key} differs from golden")


def tmpdir():
    return tempfile.TemporaryDirectory()
