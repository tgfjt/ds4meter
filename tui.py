#!/usr/bin/env python3
"""usage.db を読んでターミナルにダッシュボードを出す TUI。

    python3 tui.py              # 対話モード
    python3 tui.py --once       # 1回だけテキストで出力（色なし）

キー: 1/2/3/4/5 = 1/7/30/90/365日  tab = 下段切替  j/k ↑/↓ = 下段スクロール  r = 更新  q = 終了

curses は端末のロケール次第で全角・記号の幅を誤るので使わず、
画面バッファを ANSI エスケープで行ごとに書き出している。
"""
import argparse
import datetime as dt
import os
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from proxy import Store

HERE = Path(__file__).resolve().parent
PERIODS = {"1": (1, "1d"), "2": (7, "7d"), "3": (30, "30d"), "4": (90, "90d"), "5": (365, "1y")}
HEAT_WEEKS = 53
HEAT_DAYS = HEAT_WEEKS * 7
DAILY_TOKEN_THRESHOLDS = (100_000, 500_000, 2_000_000)
WIDE_TAIL = ""
ALT_SCREEN_ON, ALT_SCREEN_OFF = "\x1b[?1049h", "\x1b[?1049l"
CURSOR_HIDE, CURSOR_SHOW = "\x1b[?25l", "\x1b[?25h"
RESET = "\x1b[0m"
ARROWS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down"}
WEEKDAYS = "月火水木金土日"
SHORT_EP = {"chat/completions": "chat", "completions": "compl", "responses": "resp", "messages": "msgs"}
REFRESH_SEC = 5
KEYS = ("requests", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")


def compact(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(int(n))


def fmt(n):
    return "-" if n is None else f"{int(n):,}"


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s, w, right=False):
    s = str(s if s is not None else "-")
    out, cur = "", 0
    for c in s:
        cw = width(c)
        if cur + cw > w:
            if w >= 1 and out:
                while out and cur + 1 > w:
                    cur -= width(out[-1])
                    out = out[:-1]
                out += "…"
                cur += 1
            break
        out += c
        cur += cw
    pad = " " * (w - cur)
    return pad + out if right else out + pad


def fill_days(daily, n):
    by_day = {d["day"]: d for d in daily}
    today = dt.date.today()
    rows = []
    for i in range(n - 1, -1, -1):
        key = (today - dt.timedelta(days=i)).isoformat()
        rows.append(by_day.get(key) or dict({k: 0 for k in KEYS}, day=key))
    return rows


def group(rows, unit):
    groups = {}
    for r in rows:
        d = dt.date.fromisoformat(r["day"])
        if unit == "week":
            start = d - dt.timedelta(days=d.weekday())
            key, label = start.isoformat(), f"{start:%m/%d}〜"
        elif unit == "month":
            key, label = f"{d:%Y-%m}", f"{d:%Y-%m}"
        else:
            key, label = r["day"], f"{d:%m/%d} {WEEKDAYS[d.weekday()]}"
        g = groups.setdefault(key, dict({k: 0 for k in KEYS}, label=label))
        for k in KEYS:
            g[k] += r[k] or 0
    return [g for _, g in sorted(groups.items(), reverse=True) if g["requests"]]


class Canvas:
    def __init__(self, h, w):
        self.h, self.w = h, w
        self.cells = [[(" ", "")] * w for _ in range(h)]

    def put(self, y, x, text, attr=""):
        if y < 0 or y >= self.h or x >= self.w:
            return 0
        text = fit(text, min(width(text), self.w - x))
        row, col = self.cells[y], x
        for c in text:
            cw = width(c)
            if col + cw > self.w:
                break
            row[col] = (c, attr or "")
            if cw == 2:
                row[col + 1] = (WIDE_TAIL, attr or "")
            col += cw
        return col - x

    def spans(self, y, x, parts):
        for text, attr in parts:
            x += self.put(y, x, text, attr)
        return x

    def text(self):
        return "\n".join("".join(c for c, _ in r).rstrip() for r in self.cells)

    def ansi_lines(self):
        lines = []
        for r in self.cells:
            out, cur = [], ""
            for c, attr in r:
                if attr != cur:
                    out.append(RESET + (f"\x1b[{attr}m" if attr else ""))
                    cur = attr
                out.append(c)
            lines.append("".join(out) + RESET)
        return lines


class Theme:
    def __init__(self, color):
        if not color:
            self.inp = self.out = self.dim = self.bold = self.sel = self.title = self.err = ""
            self.heat = [("·", "")] + [(c, "") for c in "░▒▓█"]
            return
        self.heat = [("■", "38;5;237")] + [("■", f"38;5;{n}") for n in (24, 26, 33, 45)]
        self.inp = "38;5;33"
        self.out = "38;5;208"
        self.dim = "38;5;244"
        self.bold = "1"
        self.sel = "7"
        self.title = "1;7"
        self.err = "38;5;203"


EMPTY_GUIDE = [
    ("まだ記録がありません", "bold"),
    ("", ""),
    ("ds4-server を serve.py 経由で起動すると記録が始まります。", ""),
    ("クライアント側の設定はそのままで OK です。", ""),
    ("", ""),
    ("1. ds4-server をいったん止めて、いつものコマンドの頭に付けて起動", "bold"),
    (f"     python3 {HERE / 'serve.py'} \\", "dim"),
    ("       ./ds4-server <いつもの引数> --port 8001", "dim"),
    ("", ""),
    ("2. 試しに 1 回投げてみる", "bold"),
    ("     curl -s http://127.0.0.1:8001/v1/chat/completions \\", "dim"),
    ("       -H 'Content-Type: application/json' \\", "dim"),
    ("       -d '{\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'", "dim"),
    ("", ""),
    ("記録されると数秒でこの画面が切り替わります。", ""),
]


def _api(r):
    ep = r["endpoint"].replace("/v1/", "")
    ep = SHORT_EP.get(ep, ep) + (" ~" if r["stream"] else "")
    return f"{r['status'] or 'ERR'} {ep}" if _failed(r) else ep


def _failed(r):
    return (r["status"] or 0) >= 400 or r["error"]


def _tok_per_sec(r):
    gen = r["duration_ms"] - r["ttft_ms"] if r["ttft_ms"] is not None and r["duration_ms"] else None
    return f"{r['output_tokens'] / (gen / 1000):.1f}" if r["stream"] and r["output_tokens"] and gen and gen > 0 else "-"


class RecentCol(NamedTuple):
    header: str
    width: Optional[int]  # None は残り幅で伸縮
    right: bool
    get: Callable


class PeriodCol(NamedTuple):
    header: str
    width: int
    get: Callable


RECENT_COLS = [
    RecentCol("Time", 11, False, lambda r: time.strftime("%m/%d %H:%M", time.localtime(r["ts"]))),
    RecentCol("Client", 12, False, lambda r: r["client"]),
    RecentCol("Model", None, False, lambda r: r["model"]),
    RecentCol("API", 7, False, _api),
    RecentCol("Input", 8, True, lambda r: fmt(r["input_tokens"])),
    RecentCol("Output", 7, True, lambda r: fmt(r["output_tokens"])),
    RecentCol("Cached", 8, True, lambda r: fmt(r["cached_tokens"])),
    RecentCol("TTFT", 6, True, lambda r: f"{r['ttft_ms'] / 1000:.1f}s" if r["ttft_ms"] is not None else "-"),
    RecentCol("Dur", 6, True, lambda r: f"{r['duration_ms'] / 1000:.1f}s" if r["duration_ms"] else "-"),
    RecentCol("tok/s", 6, True, _tok_per_sec),
]
RECENT_DROP_ORDER = ("TTFT", "Cached", "Dur", "API", "tok/s")
RECENT_MODEL_MIN = 20

PERIOD_COLS = [
    PeriodCol("Req", 5, lambda g: g["requests"]),
    PeriodCol("Input", 11, lambda g: g["input_tokens"]),
    PeriodCol("Output", 9, lambda g: g["output_tokens"]),
    PeriodCol("Cached", 10, lambda g: g["cached_tokens"]),
    PeriodCol("Total", 11, lambda g: g["input_tokens"] + g["output_tokens"]),
]
PERIOD_UNITS = ((1, "hour", "Hourly"), (31, "day", "Daily"), (120, "week", "Weekly"), (None, "month", "Monthly"))
LOWER_MIN_ROWS = 7


class Dashboard:
    def __init__(self, store, days=30):
        self.store = store
        self.days = days
        self.lower = "recent"
        self.scroll = 0
        self.reload()

    def reload(self):
        self.data = self.store.stats(self.days)
        self.heat_daily = self.store.daily(HEAT_DAYS)
        midnight = time.mktime(dt.date.today().timetuple())
        self.hourly = self.store.hourly(midnight) if self.days == 1 else []
        self.loaded_at = time.time()

    def draw(self, cv, th):
        self._header(cv, th)
        if not self.data["recent"]:
            for i, (line, style) in enumerate(EMPTY_GUIDE):
                cv.put(2 + i, 2, line, getattr(th, style) if style else "")
            cv.put(cv.h - 1, 0, "q quit", th.dim)
            return
        rows = fill_days(self.data["daily"], self.days)
        y = self._summary(cv, th, 2, rows)
        y = self._heatmap(cv, th, y + 1) + 1
        y = self._period_table(cv, th, y, rows)
        self._lower(cv, th, y + 1)
        cv.put(cv.h - 1, 1, "1-5 period   tab pane   j/k scroll   r refresh   q quit", th.dim)

    def _header(self, cv, th):
        x = cv.put(0, 0, " ds4meter ", th.title) + 2
        for k, (n, label) in PERIODS.items():
            x += cv.put(0, x, f"{k}:{label}", th.sel if n == self.days else th.dim) + 2
        stamp = time.strftime("%H:%M:%S", time.localtime(self.loaded_at))
        cv.put(0, cv.w - len(stamp) - 1, stamp, th.dim)

    def _summary(self, cv, th, y, rows):
        def total(rs):
            return {k: sum(r[k] or 0 for r in rs) for k in KEYS}

        period = total(rows)
        period_label = next((lbl for n, lbl in PERIODS.values() if n == self.days), f"{self.days}d")
        lines = [("Today", total(rows[-1:]))] + ([] if self.days == 1 else [(period_label, period)])
        for label, t in lines:
            in_x = cv.spans(y, 1, [
                (fit(label, 7), th.dim),
                (fit(fmt(t["input_tokens"] + t["output_tokens"]), 11, True), th.bold), (" tok", th.dim), ("   ", th.dim),
            ])
            end = cv.spans(y, in_x, [
                ("in ", th.dim), (fmt(t["input_tokens"]), th.inp),
                ("  out ", th.dim), (fmt(t["output_tokens"]), th.out),
                ("  ", ""), (fmt(t["requests"]), ""), (" req", th.dim),
            ])
            y += 1
        hit = f"{round(period['cached_tokens'] / period['input_tokens'] * 100)}%" if period["input_tokens"] else "-"
        tps = f"{self.data['tokens_per_sec']:.1f}" if self.data["tokens_per_sec"] else "-"
        more = [("cache ", th.dim), (hit, ""), ("   ", ""), (tps, ""), (" tok/s", th.dim)]
        if end + 3 + sum(width(text) for text, _ in more) <= cv.w - 1:
            cv.spans(y - 1, end + 3, more)
        else:
            cv.spans(y, in_x, more)
            y += 1
        return y

    def _heatmap(self, cv, th, y):
        today = dt.date.today()
        weeks = max(4, min(HEAT_WEEKS, (cv.w - 5) // 2))
        start = today - dt.timedelta(days=today.weekday() + (weeks - 1) * 7)
        by_day = {r["day"]: r["input_tokens"] + r["output_tokens"] for r in self.heat_daily}
        x0 = 4
        last_label_end = 0
        for c in range(weeks):
            monday = start + dt.timedelta(days=c * 7)
            x = x0 + c * 2
            if (c == 0 or monday.month != (monday - dt.timedelta(days=7)).month) and x >= last_label_end:
                last_label_end = x + cv.put(y, x, f"{monday.month}月", th.dim) + 1
            for r in range(7):
                day = monday + dt.timedelta(days=r)
                if day > today:
                    continue
                v = by_day.get(day.isoformat(), 0)
                level = 0 if not v else 1 + sum(v >= step for step in DAILY_TOKEN_THRESHOLDS)
                ch, attr = th.heat[level]
                cv.put(y + 1 + r, x, ch, attr)
        for r, name in ((0, "月"), (2, "水"), (4, "金")):
            cv.put(y + 1 + r, 1, name, th.dim)
        y += 8
        span = sum(by_day.get((start + dt.timedelta(days=i)).isoformat(), 0) for i in range((today - start).days + 1))
        total = [(f"{start:%Y/%m/%d} から今日まで ", th.dim), (fmt(span), th.bold), (" tok", th.dim)]
        labels = ["0"] + [f"<{compact(b)}" for b in DAILY_TOKEN_THRESHOLDS] + [f"{compact(DAILY_TOKEN_THRESHOLDS[-1])}+"]
        legend = []
        for (ch, attr), label in zip(th.heat, labels):
            legend += [(ch, attr), (f" {label}  ", th.dim)]
        total_w, legend_w = (sum(width(t) for t, _ in parts) for parts in (total, legend))
        cv.spans(y, max(x0, x0 + weeks * 2 - legend_w), legend)
        if total_w + 2 + legend_w > weeks * 2:
            y += 1
        cv.spans(y, x0, total)
        return y + 1

    def _period_table(self, cv, th, y, rows):
        unit, title = next((u, t) for limit, u, t in PERIOD_UNITS if limit is None or self.days <= limit)
        if unit == "hour":
            groups = [dict(h, label=f"{int(h['hour'])}:00") for h in reversed(self.hourly)]
        else:
            groups = group(rows, unit)
        table_h = max(2, cv.h - y - 1 - LOWER_MIN_ROWS)
        cols = [c for c in PERIOD_COLS if cv.w >= 90 or c.header != "Cached"]
        label_w = 10
        bar_x = 1 + label_w + sum(cw + 1 for _, cw, _ in cols) + 1
        bar_w = max(0, cv.w - bar_x - 1)
        cv.spans(y, 1, [(fit(title, label_w), th.bold)] + [(" " + fit(name, cw, True), th.dim) for name, cw, _ in cols])
        legend = [("■", th.inp), (" in  ", th.dim), ("■", th.out), (" out", th.dim)]
        for extra in ([], [("   単位: tokens", th.dim)]):
            if sum(width(t) for t, _ in legend + extra) <= bar_w:
                legend += extra
            else:
                legend = legend if extra else []
                break
        cv.spans(y, bar_x, legend)
        y += 1
        gmax = max((g["input_tokens"] + g["output_tokens"] for g in groups), default=0) or 1
        shown = groups if len(groups) <= table_h - 1 else groups[:max(1, table_h - 2)]
        for g in shown:
            cv.spans(y, 1, [(fit(g["label"], label_w), "")] +
                     [(" " + fit(fmt(get(g)), cw, True), th.bold if name == "Total" else "") for name, cw, get in cols])
            if bar_w >= 6:
                bi = round(g["input_tokens"] / gmax * bar_w)
                bo = round(g["output_tokens"] / gmax * bar_w)
                if g["output_tokens"] and not bo:
                    bo = 1
                cv.spans(y, bar_x, [("█" * bi, th.inp), ("█" * bo, th.out)])
            y += 1
        if not groups:
            cv.put(y, 1, "この期間の記録はありません", th.dim)
            y += 1
        elif len(groups) > len(shown):
            cv.put(y, 1, f"… ほか {len(groups) - len(shown)} 件", th.dim)
            y += 1
        return y

    def _lower(self, cv, th, y):
        x = 1
        for key, label in (("recent", "Recent"), ("breakdown", "By client / model")):
            x += cv.put(y, x, f" {label} ", th.sel if key == self.lower else th.dim) + 1
        x += cv.put(y, x + 1, "(tab)", th.dim) + 1
        avail = cv.h - y - 2
        if self.lower == "recent":
            total, visible = len(self.data["recent"]), avail - 1
        else:
            total = max(len(self.data["by_client"]), len(self.data["by_model"]))
            visible = avail - 1 if cv.w // 2 >= 40 else min(max(2, avail // 2), avail - max(2, avail // 2)) - 1
        self.scroll = max(0, min(self.scroll, total - max(1, visible)))
        if total > visible > 0:
            cv.put(y, x + 2, f"{self.scroll + 1}-{min(total, self.scroll + visible)} / {total}  (j/k)", th.dim)
        y += 1
        if self.lower == "recent":
            cv.put(y - 1, cv.w - 15, fit("単位: tokens", 14, True), th.dim)
            self._recent(cv, th, y, avail)
            return
        half = cv.w // 2
        if half >= 40:
            self._breakdown(cv, th, y, 1, half - 2, avail, "Client", self.data["by_client"])
            self._breakdown(cv, th, y, half + 1, cv.w - half - 2, avail, "Model", self.data["by_model"])
        else:
            n = max(2, avail // 2)
            self._breakdown(cv, th, y, 1, cv.w - 2, n, "Client", self.data["by_client"])
            self._breakdown(cv, th, y + n, 1, cv.w - 2, avail - n, "Model", self.data["by_model"])

    def _breakdown(self, cv, th, y, x, w, h, title, rows):
        name_w = max(8, w - 6 - 10 - 10 - 3)
        cv.put(y, x, fit(title, name_w) + fit("Req", 6, True) + fit("Input", 10, True) + fit("Output", 10, True), th.dim)
        if not rows:
            cv.put(y + 1, x, "この期間の記録はありません", th.dim)
        for i, r in enumerate(rows[self.scroll:self.scroll + max(0, h - 1)]):
            cv.put(y + 1 + i, x, fit(r["name"], name_w) + fit(fmt(r["requests"]), 6, True)
                   + fit(fmt(r["input_tokens"]), 10, True) + fit(fmt(r["output_tokens"]), 10, True))

    def _recent(self, cv, th, y, h):
        cols = RECENT_COLS
        for drop in RECENT_DROP_ORDER:
            if sum((c.width or 0) + 1 for c in cols) + RECENT_MODEL_MIN <= cv.w - 2:
                break
            cols = [c for c in cols if c.header != drop]
        model_w = max(8, min(28, cv.w - 2 - sum((c.width or 0) + 1 for c in cols) - 1))
        cols = [c._replace(width=model_w) if c.width is None else c for c in cols]
        cv.put(y, 1, "".join(fit(name, cw, right) + " " for name, cw, right, _ in cols), th.dim)
        for i, r in enumerate(self.data["recent"][self.scroll:self.scroll + max(0, h - 1)]):
            line = "".join(fit(get(r), cw, right) + " " for _, cw, right, get in cols)
            cv.put(y + 1 + i, 1, line, th.err if _failed(r) else "")

    def key(self, k):
        if k in ("q", "esc"):
            return False
        if k in PERIODS:
            self.days, self.scroll = PERIODS[k][0], 0
            self.reload()
        elif k == "r":
            self.reload()
        elif k == "\t":
            self.lower, self.scroll = ("breakdown" if self.lower == "recent" else "recent"), 0
        elif k in ("j", "down"):
            self.scroll += 1  # 上限は描画時に丸める
        elif k in ("k", "up"):
            self.scroll = max(0, self.scroll - 1)
        return True


def read_keys(fd, timeout):
    if not select.select([fd], [], [], timeout)[0]:
        return []
    data = os.read(fd, 64).decode("utf-8", "ignore")
    keys, i = [], 0
    while i < len(data):
        seq = data[i:i + 3]
        if seq in ARROWS:
            keys.append(ARROWS[seq])
            i += 3
        elif data[i] == "\x1b":
            # 単独の ESC だけを終了扱いにし、それ以外の未知のエスケープ列は読み捨てる
            if i + 1 == len(data):
                keys.append("esc")
            break
        else:
            keys.append(data[i])
            i += 1
    return keys


def run(dash):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    th = Theme(True)
    out = sys.stdout
    out.write(ALT_SCREEN_ON + CURSOR_HIDE)
    last_frame = None
    try:
        tty.setcbreak(fd)
        while True:
            if time.time() - dash.loaded_at >= REFRESH_SEC:
                dash.reload()
            w, h = shutil.get_terminal_size()
            if h < 20 or w < 60:
                frame = f"\x1b[H\x1b[2Jterminal too small ({w}x{h}, need 60x20)"
            else:
                cv = Canvas(h, w)
                dash.draw(cv, th)
                frame = "\x1b[H" + "\x1b[K\r\n".join(cv.ansi_lines()) + "\x1b[K"
            if frame != last_frame:
                out.write(frame)
                out.flush()
                last_frame = frame
            for k in read_keys(fd, 0.25):
                if not dash.key(k):
                    return
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write(RESET + CURSOR_SHOW + ALT_SCREEN_OFF)
        out.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HERE / "usage.db"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--once", action="store_true", help="print one snapshot as plain text and exit")
    ap.add_argument("--size", default="120x40", help="snapshot size for --once (WxH)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"{args.db} がありません。先に serve.py で ds4-server を起動してください。")
    dash = Dashboard(Store(args.db), args.days)
    if args.once:
        w, h = (int(v) for v in args.size.split("x"))
        cv = Canvas(h, w)
        dash.draw(cv, Theme(False))
        print(cv.text())
        return
    run(dash)


if __name__ == "__main__":
    main()
