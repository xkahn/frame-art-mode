import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from frame_art_mode.main import require_supported_python


class PythonRuntimeTests(unittest.TestCase):
    def test_python_38_is_rejected_with_recreation_guidance(self):
        with self.assertRaisesRegex(RuntimeError, "Python 3.10 or later"):
            require_supported_python((3, 8))

    def test_python_310_or_newer_is_accepted(self):
        require_supported_python((3, 10))
        require_supported_python((3, 11))

    def test_startup_and_docs_require_fresh_python311_venv(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        launcher = (root / "start.sh").read_text(encoding="utf-8")
        self.assertIn("python3.11 -m venv .venv", readme)
        self.assertIn("created by Python 3.8", readme)
        self.assertIn("Python 3.10 or later", readme)
        self.assertIn('sys.version_info < (3, 10)', launcher)
        self.assertIn("Recreate", launcher)

    def test_launcher_rejects_simulated_python38_and_accepts_311(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".venv" / "bin").mkdir(parents=True)
            shutil.copy2(source_root / "start.sh", root / "start.sh")
            os.chmod(root / "start.sh", 0o755)
            python = root / ".venv" / "bin" / "python"
            python.write_text(
                """#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$*" in
        *major*minor*) echo "3.8"; exit 0 ;;
        *sys.version_info*) exit 1 ;;
    esac
fi
exit 0
""",
                encoding="utf-8",
            )
            python.chmod(0o755)
            rejected = subprocess.run(
                [str(root / "start.sh")],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("Recreate", rejected.stderr)
            self.assertIn("Python 3.8", rejected.stderr)

            python.write_text(
                """#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$*" in
        *major*minor*) echo "3.11"; exit 0 ;;
        *sys.version_info*) exit 0 ;;
    esac
fi
exit 0
""",
                encoding="utf-8",
            )
            python.chmod(0o755)
            accepted = subprocess.run(
                [str(root / "start.sh")],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(accepted.returncode, 0)


if __name__ == "__main__":
    unittest.main()
