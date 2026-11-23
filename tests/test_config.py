import tempfile
import unittest
from pathlib import Path

from frame_art_mode.config import load_config


class ConfigTests(unittest.TestCase):
    def test_requires_specific_tv_and_bind_ips_and_cli_overrides_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                '{"tv_ip":"192.168.1.50","bind_ip":"192.168.1.20","port":8080}',
                encoding="utf-8",
            )
            config = load_config(config_file, {"port": 9090}, script_dir=root)
            self.assertEqual(config.port, 9090)
            self.assertEqual(config.image_root, root / "image")
            with self.assertRaisesRegex(ValueError, "wildcard"):
                load_config(config_file, {"bind_ip": "0.0.0.0"}, script_dir=root)


if __name__ == "__main__":
    unittest.main()
