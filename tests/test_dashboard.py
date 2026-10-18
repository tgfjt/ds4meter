import re
import subprocess
import sys
import unittest
from unittest import mock

from support import DATASETS, REPO, check_golden, frozen_clock, make_store, tmpdir, tui

SIZES = ((60, 20), (70, 30), (80, 30), (120, 44))
ANSI_SIZE = (80, 30)
DAYS = (1, 7, 30, 365)
PANES = ("recent", "breakdown")


def render_all():
    out = {}
    guide = [(line.replace(str(REPO), "<repo>"), style) for line, style in tui.EMPTY_GUIDE]
    with tmpdir() as d, frozen_clock(), mock.patch.object(tui, "EMPTY_GUIDE", guide):
        for dataset in DATASETS:
            store = make_store(dataset, d)
            for days in DAYS:
                dash = tui.Dashboard(store, days)
                for pane in PANES:
                    dash.lower = pane
                    for w, h in SIZES:
                        for color in (False, True) if (w, h) == ANSI_SIZE else (False,):
                            cv = tui.Canvas(h, w)
                            dash.draw(cv, tui.Theme(color))
                            screen = "\n".join(cv.ansi_lines()) if color else cv.text()
                            out[f"{dataset}/{days}d/{pane}/{w}x{h}/{'ansi' if color else 'plain'}"] = screen
    return out


class DashboardSnapshot(unittest.TestCase):
    def test_screens_match_golden(self):
        check_golden(self, "dashboard", render_all())

    def test_every_line_fits_terminal_width(self):
        for key, screen in render_all().items():
            if key.endswith("/plain"):
                w = int(key.split("/")[3].split("x")[0])
                for line in screen.split("\n"):
                    self.assertLessEqual(tui.width(line), w, f"{key}: {line!r}")


class Layout(unittest.TestCase):
    def screens(self):
        with tmpdir() as d, frozen_clock():
            for dataset in ("one", "levels", "seed"):
                dash = tui.Dashboard(make_store(dataset, d), 30)
                for w in range(60, 141):
                    cv = tui.Canvas(40, w)
                    dash.draw(cv, tui.Theme(False))
                    yield f"{dataset} w={w}", cv.text().split("\n")

    def test_heatmap_total_and_legend_are_never_cut(self):
        for name, lines in self.screens():
            line = next(l for l in lines if "Since " in l)
            self.assertRegex(line, r"Since \d{4}-\d\d-\d\d: [\d,]+ tok", name)
            self.assertTrue(any("· 0  ░ <100K  ▒ <500K  ▓ <2M  █ 2M+" in l for l in lines), f"{name}: heatmap legend")

    def test_month_labels_are_english_abbreviations(self):
        months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
        for name, lines in self.screens():
            row = next(i for i, l in enumerate(lines) if l.startswith("Mon")) - 1
            words = lines[row].split()
            self.assertTrue(words and set(words) <= months, f"{name}: {lines[row]!r}")

    def test_table_legend_is_whole_or_absent(self):
        for name, lines in self.screens():
            header = next(l for l in lines if l.startswith((" Daily", " Weekly", " Monthly", " Hourly")))
            self.assertNotIn("…", header, name)

    def test_weekday_labels_are_separated_from_cells(self):
        for name, lines in self.screens():
            labelled = [l for l in lines if l.startswith(("Mon", "Wed", "Fri"))]
            self.assertEqual(len(labelled), 3, f"{name}: weekday labels start at column 0")
            for l in labelled:
                self.assertRegex(l, r"^(Mon|Wed|Fri) [·░▒▓█]", name)


class English(unittest.TestCase):
    CJK = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef]")

    def test_screens_have_no_japanese(self):
        for key, screen in render_all().items():
            self.assertIsNone(self.CJK.search(screen), f"{key}: {self.CJK.findall(screen)[:5]}")

    def test_help_and_errors_have_no_japanese(self):
        outputs = [subprocess.run([sys.executable, str(REPO / s), "--help"], capture_output=True, text=True)
                   for s in ("serve.py", "proxy.py", "tui.py")]
        outputs.append(subprocess.run([sys.executable, str(REPO / "tui.py"), "--db", "/nonexistent.db"],
                                      capture_output=True, text=True))
        for r in outputs:
            text = r.stdout + r.stderr
            self.assertIsNone(self.CJK.search(text), f"{r.args}: {self.CJK.findall(text)[:5]}")


class Scroll(unittest.TestCase):
    def screen(self, dash, h=30):
        cv = tui.Canvas(h, 100)
        dash.draw(cv, tui.Theme(False))
        return cv.text().split("\n")

    def test_j_reveals_older_requests_and_stops_at_the_end(self):
        with tmpdir() as d, frozen_clock():
            dash = tui.Dashboard(make_store("seed", d), 30)
            first = self.screen(dash)
            self.assertIn("1-", next(l for l in first if "By client / model" in l))
            dash.key("j")
            after = self.screen(dash)
            self.assertNotEqual(first, after)
            self.assertIn("2-", next(l for l in after if "By client / model" in l))
            for _ in range(10_000):
                dash.key("j")
            last = self.screen(dash)
            total = len(dash.data["recent"])
            self.assertIn(f"-{total} / {total}", next(l for l in last if "By client / model" in l))

    def test_switching_pane_or_period_returns_to_top(self):
        with tmpdir() as d, frozen_clock():
            dash = tui.Dashboard(make_store("seed", d), 30)
            dash.key("j"); dash.key("j"); dash.key("\t")
            self.assertEqual(dash.scroll, 0)
            dash.key("j"); dash.key("1")
            self.assertEqual(dash.scroll, 0)


class Keys(unittest.TestCase):
    def test_arrow_up_down_are_scroll_keys(self):
        import os
        r, w = os.pipe()
        os.write(w, b"\x1b[B\x1b[Aj")
        self.assertEqual(tui.read_keys(r, 0.1), ["down", "up", "j"])

    def test_lone_escape_quits_but_arrow_sequences_are_ignored(self):
        import os
        r, w = os.pipe()
        os.write(w, b"1\t\x1b[D")
        self.assertEqual(tui.read_keys(r, 0.1), ["1", "\t"])
        os.write(w, b"\x1b")
        self.assertEqual(tui.read_keys(r, 0.1), ["esc"])


if __name__ == "__main__":
    unittest.main()
