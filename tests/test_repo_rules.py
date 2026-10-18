import ast
import importlib.util
import re
import subprocess
import sys
import unittest

from support import REPO

SOURCES = sorted(REPO.glob("*.py"))
LOCAL_MODULES = {p.stem for p in SOURCES}

# 運用者が承認した CLI フラグ。この一覧の変更は運用者の承認が要る（CLAUDE.md のルール表）
APPROVED_FLAGS = {
    "proxy.py": {"-h", "--help", "--listen", "--upstream", "--db"},
    "tui.py": {"-h", "--help", "--db", "--days", "--once", "--size"},
}


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module.split(".")[0]


def is_stdlib(name):
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    origin = (spec and spec.origin) or ""
    return origin.startswith(sys.base_prefix) and "site-packages" not in origin


class RepoRules(unittest.TestCase):
    def test_stdlib_only(self):
        for path in SOURCES:
            for name in imported_modules(path):
                if name in LOCAL_MODULES:
                    continue
                self.assertTrue(is_stdlib(name), f"{path.name} imports '{name}'. Runtime code must use the "
                                                 "Python standard library only, so it runs on a bare macOS python3.")

    def test_no_unapproved_cli_flags(self):
        for script, approved in APPROVED_FLAGS.items():
            help_text = subprocess.run([sys.executable, str(REPO / script), "--help"],
                                       capture_output=True, text=True, check=True).stdout
            extra = set(re.findall(r"(?<![\w-])(-h|--[a-z][\w-]*)", help_text)) - approved
            self.assertFalse(extra, f"{script} gained CLI flags {sorted(extra)}. Stop and ask the operator. "
                                    "Use a constant in code instead of a flag unless the operator asks for the flag.")


if __name__ == "__main__":
    unittest.main()
