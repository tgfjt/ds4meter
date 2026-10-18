"""Render the dashboard with demo data to docs/screenshot.svg.

    cd tests && python3 screenshot.py
"""
import random
from html import escape

from support import NOW, REPO, frozen_clock, proxy, tmpdir, tui

COLS, ROWS = 100, 42
CELL_W, CELL_H, FONT = 8.4, 17, 14
SQUARE = 12
FG, BG = "#c9d1d9", "#0d1117"
OUT = REPO / "docs" / "screenshot.svg"


def demo_rows():
    rng = random.Random(7)
    rows = []
    for day in range(140):
        if day > 0 and rng.random() < 0.25:
            continue
        for _ in range(rng.randint(30, 90) if day else 40):
            ts = NOW - day * 86400 - rng.uniform(0, 11 * 3600 if day == 0 else 14 * 3600)
            inp, out, rate = rng.randint(6000, 36000), rng.randint(80, 3500), rng.uniform(44, 56)
            ttft = rng.uniform(500, 3000)
            rows.append({
                "ts": ts, "endpoint": rng.choice(["/v1/chat/completions"] * 6 + ["/v1/messages"] * 3 + ["/v1/responses"]),
                "model": rng.choice(["qwen3.8-flash-next"] * 3 + ["deepseek-v4-flash"]),
                "client": rng.choice(["pi"] * 6 + ["claude-cli"] * 3 + ["codex_cli_rs"]),
                "stream": 1, "status": 200, "input_tokens": inp, "output_tokens": out,
                "cached_tokens": int(inp * rng.uniform(0.3, 0.95)), "reasoning_tokens": out // 4,
                "duration_ms": ttft + out / rate * 1000, "ttft_ms": ttft, "error": None,
            })
    rows.append({"ts": NOW - 300, "endpoint": "/v1/chat/completions", "model": "qwen3.8-flash-next", "client": "pi",
                 "stream": 1, "status": 200, "duration_ms": 23677, "error": f"{proxy.CLIENT_DISCONNECTED}: Broken pipe"})
    return rows


def xterm(n):
    base = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
            "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (levels[n // 36], levels[n // 6 % 6], levels[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def style(attr):
    codes = [int(c) for c in attr.split(";")] if attr else []
    fg, bg, bold, reverse = FG, None, False, False
    i = 0
    while i < len(codes):
        if codes[i] in (38, 48) and i + 2 < len(codes):
            if codes[i] == 38:
                fg = xterm(codes[i + 2])
            else:
                bg = xterm(codes[i + 2])
            i += 3
            continue
        bold |= codes[i] == 1
        reverse |= codes[i] == 7
        i += 1
    if reverse:
        fg, bg = bg or BG, fg
    return fg, bg, bold


def svg(cv):
    w, h = cv.w * CELL_W + 24, cv.h * CELL_H + 24
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h}" viewBox="0 0 {w:.0f} {h}">',
           f'<rect width="100%" height="100%" rx="8" fill="{BG}"/>',
           f'<g transform="translate(12 12)" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="{FONT}">']
    for y, row in enumerate(cv.cells):
        x = 0
        while x < cv.w:
            ch, attr = row[x]
            span = 1
            while x + span < cv.w and row[x + span][1] == attr and row[x + span][0] not in "█■" and ch not in "█■":
                span += 1
            fg, bg, bold = style(attr)
            px, py = x * CELL_W, y * CELL_H
            if bg:
                out.append(f'<rect x="{px:.1f}" y="{py}" width="{span * CELL_W:.1f}" height="{CELL_H}" fill="{bg}"/>')
            if ch == "█":
                out.append(f'<rect x="{px:.1f}" y="{py + 1}" width="{CELL_W + 0.1:.1f}" height="{CELL_H - 2}" fill="{fg}"/>')
            elif ch == "■":
                cx, cy = px + CELL_W / 2, py + CELL_H / 2
                out.append(f'<rect x="{cx - SQUARE / 2:.1f}" y="{cy - SQUARE / 2:.1f}" width="{SQUARE}" height="{SQUARE}" rx="2" fill="{fg}"/>')
            else:
                text = "".join(c for c, _ in row[x:x + span])
                if text.strip():
                    weight = ' font-weight="bold"' if bold else ""
                    out.append(f'<text x="{px:.1f}" y="{py + CELL_H - 4}" fill="{fg}"{weight} xml:space="preserve" '
                               f'textLength="{span * CELL_W:.1f}" lengthAdjust="spacingAndGlyphs">{escape(text)}</text>')
            x += span
    out.append("</g></svg>")
    return "\n".join(out) + "\n"


def main():
    with tmpdir() as d, frozen_clock():
        store = proxy.Store(f"{d}/demo.db")
        for row in demo_rows():
            store.insert(row)
        dash = tui.Dashboard(store, 7)
        cv = tui.Canvas(ROWS, COLS)
        dash.draw(cv, tui.Theme(True))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(svg(cv))
    print(OUT)


if __name__ == "__main__":
    main()
