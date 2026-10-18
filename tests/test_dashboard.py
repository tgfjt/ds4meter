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
