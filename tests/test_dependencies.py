import unittest
from pathlib import Path


class DependencyDeclarationTests(unittest.TestCase):
    def test_tv_library_is_pinned_to_upstream_release(self):
        requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "samsungframetv @ git+https://github.com/NickWaterton/samsung-tv-ws-api.git@4.1.4",
            requirements,
        )
        self.assertNotIn("samsungframetv>=4.0", requirements)

    def test_install_docs_require_compatible_python_and_document_source(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Python 3.10 or later", readme)
        self.assertIn("4.1.4", readme)
        self.assertIn("NickWaterton/samsung-tv-ws-api", readme)


if __name__ == "__main__":
    unittest.main()
