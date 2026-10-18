import re
import subprocess
import sys
import unittest

from support import REPO

# 運用者が承認した CLI フラグ。この一覧の変更は運用者の承認が要る
APPROVED_FLAGS = {
    "proxy.py": {"-h", "--help", "--listen", "--upstream", "--db"},
    "tui.py": {"-h", "--help", "--db", "--days", "--once", "--size"},
}


class RepoRules(unittest.TestCase):
    def test_no_unapproved_cli_flags(self):
        for script, approved in APPROVED_FLAGS.items():
            help_text = subprocess.run([sys.executable, str(REPO / script), "--help"],
                                       capture_output=True, text=True, check=True).stdout
            extra = set(re.findall(r"(?<![\w-])(-h|--[a-z][\w-]*)", help_text)) - approved
            self.assertFalse(extra, f"{script} gained CLI flags {sorted(extra)}. Stop and ask the operator. "
                                    "Use a constant in code instead of a flag unless the operator asks for the flag.")


if __name__ == "__main__":
    unittest.main()
