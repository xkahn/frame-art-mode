import ast
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from frame_art_mode.config import ConfigError, load_config, loads_jsonc


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

    def test_backwards_compatible_five_key_config_and_derived_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "tv_ip": "192.168.1.50",
                        "bind_ip": "192.168.1.20",
                        "port": 8080,
                        "image_root": "photos",
                        "state_dir": "runtime_state",
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_file, script_dir=root)
            expected_tv_key = hashlib.sha256(b"192.168.1.50").hexdigest()[:16]
            self.assertEqual(config.image_root, (root / "photos").resolve())
            self.assertEqual(config.state_dir, (root / "runtime_state").resolve())
            self.assertEqual(config.cache_dir, (root / "runtime_state" / "cache").resolve())
            self.assertEqual(config.preview_seconds, 15)
            self.assertEqual(config.max_image_bytes, 5242880)
            self.assertEqual(config.tv_op_delay_seconds, 3.0)
            self.assertEqual(config.tv_batch_settle_seconds, 12.0)
            self.assertEqual(config.tv_timeout_seconds, 30.0)
            self.assertEqual(config.tv_verify_retries, 4)
            self.assertEqual(config.tv_verify_interval_seconds, 3.0)
            self.assertEqual(config.max_set_images_warning, 50)
            self.assertEqual(config.tv_key, expected_tv_key)
            self.assertEqual(config.tv_dir, config.state_dir / expected_tv_key)
            self.assertEqual(config.library_sock, config.state_dir / "library.sock")
            self.assertEqual(config.tv_sock, config.state_dir / expected_tv_key / "tv.sock")
            self.assertEqual(config.library_lock, config.state_dir / "library.lock")
            self.assertEqual(config.tv_lock, config.state_dir / expected_tv_key / "tv.lock")
            self.assertEqual(config.supervisor_pid, config.state_dir / "supervisor.pid")

    def test_optional_keys_and_relative_cache_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg_dir = root / "conf"
            cfg_dir.mkdir()
            config_file = cfg_dir / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "tv_ip": "192.168.1.50",
                        "bind_ip": "192.168.1.20",
                        "cache_dir": "../custom_cache",
                        "preview_seconds": 30,
                        "max_image_bytes": 1048576,
                        "tv_op_delay_seconds": 2,
                        "tv_batch_settle_seconds": 0,
                        "tv_timeout_seconds": 45.5,
                        "tv_verify_retries": 6,
                        "tv_verify_interval_seconds": 1.5,
                        "max_set_images_warning": 25,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_file, script_dir=root)
            self.assertEqual(config.cache_dir, (root / "custom_cache").resolve())
            self.assertEqual(config.preview_seconds, 30)
            self.assertEqual(config.max_image_bytes, 1048576)
            self.assertEqual(config.tv_op_delay_seconds, 2.0)
            self.assertIsInstance(config.tv_op_delay_seconds, float)
            self.assertEqual(config.tv_batch_settle_seconds, 0.0)
            self.assertEqual(config.tv_timeout_seconds, 45.5)
            self.assertEqual(config.tv_verify_retries, 6)
            self.assertEqual(config.tv_verify_interval_seconds, 1.5)
            self.assertEqual(config.max_set_images_warning, 25)

    def test_rejects_bool_as_number_on_all_numeric_fields(self):
        numeric_fields = (
            "port",
            "preview_seconds",
            "max_image_bytes",
            "tv_op_delay_seconds",
            "tv_batch_settle_seconds",
            "tv_timeout_seconds",
            "tv_verify_retries",
            "tv_verify_interval_seconds",
            "max_set_images_warning",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps({"tv_ip": "192.168.1.50", "bind_ip": "192.168.1.20"}),
                encoding="utf-8",
            )
            for field_name in numeric_fields:
                for bad_value in (True, False):
                    with self.subTest(field=field_name, value=bad_value):
                        with self.assertRaises(ConfigError):
                            load_config(config_file, {field_name: bad_value}, script_dir=root)

    def test_rejects_invalid_numeric_bounds(self):
        invalid_cases = [
            ("port", 0),
            ("port", 65536),
            ("port", 8080.5),
            ("preview_seconds", 0),
            ("max_image_bytes", 262143),
            ("tv_op_delay_seconds", 0.49),
            ("tv_batch_settle_seconds", -0.1),
            ("tv_timeout_seconds", 4.9),
            ("tv_verify_retries", 0),
            ("tv_verify_interval_seconds", 0.4),
            ("max_set_images_warning", 0),
            ("tv_timeout_seconds", float("nan")),
            ("tv_timeout_seconds", float("inf")),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps({"tv_ip": "192.168.1.50", "bind_ip": "192.168.1.20"}),
                encoding="utf-8",
            )
            for field_name, bad_value in invalid_cases:
                with self.subTest(field=field_name, value=bad_value):
                    with self.assertRaises(ConfigError):
                        load_config(config_file, {field_name: bad_value}, script_dir=root)

    def test_rejects_state_dir_and_cache_dir_inside_or_equal_to_image_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_root = root / "images"
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps({"tv_ip": "192.168.1.50", "bind_ip": "192.168.1.20"}),
                encoding="utf-8",
            )
            for bad_state in (image_root, image_root / "nested"):
                with self.assertRaisesRegex(ConfigError, "state_dir"):
                    load_config(
                        config_file,
                        {"image_root": str(image_root), "state_dir": str(bad_state)},
                        script_dir=root,
                    )
            for bad_cache in (image_root, image_root / "nested_cache"):
                with self.assertRaisesRegex(ConfigError, "cache_dir"):
                    load_config(
                        config_file,
                        {
                            "image_root": str(image_root),
                            "state_dir": str(root / "state"),
                            "cache_dir": str(bad_cache),
                        },
                        script_dir=root,
                    )

    def test_rejects_unspecified_multicast_and_broadcast_ips(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps({"tv_ip": "192.168.1.50", "bind_ip": "192.168.1.20"}),
                encoding="utf-8",
            )
            for bad_ip in ("0.0.0.0", "::", "224.0.0.1", "ff02::1", "255.255.255.255"):
                with self.subTest(bind_ip=bad_ip):
                    with self.assertRaises(ConfigError):
                        load_config(config_file, {"bind_ip": bad_ip}, script_dir=root)
                with self.subTest(tv_ip=bad_ip):
                    with self.assertRaises(ConfigError):
                        load_config(config_file, {"tv_ip": bad_ip}, script_dir=root)

    def test_symlink_loop_raises_config_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps({"tv_ip": "192.168.1.50", "bind_ip": "192.168.1.20"}),
                encoding="utf-8",
            )
            loop_a = root / "loop_a"
            loop_b = root / "loop_b"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)

            for field_name in ("image_root", "state_dir", "cache_dir"):
                with self.subTest(field=field_name):
                    with self.assertRaisesRegex(ConfigError, "cannot resolve"):
                        load_config(config_file, {field_name: str(loop_a)}, script_dir=root)

            with self.assertRaisesRegex(ConfigError, "cannot resolve"):
                load_config(config_file, script_dir=loop_a)

            with self.assertRaisesRegex(ConfigError, "cannot resolve"):
                load_config(loop_a / "config.json", script_dir=root)

    def test_config_example_contains_all_documented_keys_and_loads(self):
        repo_root = Path(__file__).resolve().parents[1]
        example_path = repo_root / "config.example.json"
        self.assertTrue(example_path.is_file())

        raw = loads_jsonc(example_path.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, dict)
        expected_keys = {
            "tv_ip",
            "bind_ip",
            "port",
            "image_root",
            "state_dir",
            "cache_dir",
            "preview_seconds",
            "max_image_bytes",
            "tv_op_delay_seconds",
            "tv_batch_settle_seconds",
            "tv_timeout_seconds",
            "tv_verify_retries",
            "tv_verify_interval_seconds",
            "max_set_images_warning",
        }
        self.assertEqual(set(raw), expected_keys)

        config = load_config(example_path, script_dir=repo_root)
        expected_image_root = Path("/volume1/photo/FrameArt").resolve()
        expected_state_dir = Path("/volume1/applications/frame-art-mode-state").resolve()
        expected_cache_dir = Path("/volume1/applications/frame-art-mode-state/cache").resolve()

        self.assertIs(type(config.tv_ip), str)
        self.assertEqual(config.tv_ip, "192.168.1.50")
        self.assertIs(type(config.bind_ip), str)
        self.assertEqual(config.bind_ip, "192.168.1.20")
        self.assertIs(type(config.port), int)
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.image_root, expected_image_root)
        self.assertEqual(config.state_dir, expected_state_dir)
        self.assertEqual(config.cache_dir, expected_cache_dir)
        self.assertNotEqual(config.state_dir, config.image_root)
        self.assertFalse(config.state_dir.is_relative_to(config.image_root))
        self.assertNotEqual(config.cache_dir, config.image_root)
        self.assertFalse(config.cache_dir.is_relative_to(config.image_root))
        self.assertIs(type(config.preview_seconds), int)
        self.assertEqual(config.preview_seconds, 15)
        self.assertIs(type(config.max_image_bytes), int)
        self.assertEqual(config.max_image_bytes, 5242880)
        self.assertIs(type(config.tv_op_delay_seconds), float)
        self.assertEqual(config.tv_op_delay_seconds, 3.0)
        self.assertIs(type(config.tv_batch_settle_seconds), float)
        self.assertEqual(config.tv_batch_settle_seconds, 12.0)
        self.assertIs(type(config.tv_timeout_seconds), float)
        self.assertEqual(config.tv_timeout_seconds, 30.0)
        self.assertIs(type(config.tv_verify_retries), int)
        self.assertEqual(config.tv_verify_retries, 4)
        self.assertIs(type(config.tv_verify_interval_seconds), float)
        self.assertEqual(config.tv_verify_interval_seconds, 3.0)
        self.assertIs(type(config.max_set_images_warning), int)
        self.assertEqual(config.max_set_images_warning, 50)

    def test_config_error_is_value_error_subclass(self):
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_loads_jsonc_comments_trailing_commas_and_bom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                "\ufeff"
                "// leading comment\n"
                "{\n"
                "  /* block comment\n"
                "     spanning lines */\n"
                '  "tv_ip": "192.168.1.50", // inline comment\n'
                '  "bind_ip": "192.168.1.20", /* inline block */\n'
                '  "port": 8090,\n'
                "}\n"
                "// trailing comment\n",
                encoding="utf-8",
            )
            config = load_config(config_file, script_dir=root)
            self.assertEqual(config.tv_ip, "192.168.1.50")
            self.assertEqual(config.bind_ip, "192.168.1.20")
            self.assertEqual(config.port, 8090)

    def test_loads_jsonc_preserves_comment_tokens_and_escapes_in_strings(self):
        payload = (
            "{\n"
            '  "url": "http://example.com//path/*kept*/",\n'
            '  "escaped": "quote \\" // not a comment /* also kept */",\n'
            '  "backslash_end": "C:\\\\path\\\\", // real trailing comment\n'
            '  "arr": [1, 2, ],\n'
            "}\n"
        )
        parsed = loads_jsonc(payload)
        self.assertEqual(
            parsed,
            {
                "url": "http://example.com//path/*kept*/",
                "escaped": 'quote " // not a comment /* also kept */',
                "backslash_end": "C:\\path\\",
                "arr": [1, 2],
            },
        )

    def test_loads_jsonc_rejects_unterminated_block_comment_and_bad_commas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.json"
            config_file.write_text(
                '{\n  "tv_ip": "192.168.1.50", /* unclosed\n  "bind_ip": "192.168.1.20"\n}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unterminated block comment"):
                load_config(config_file, script_dir=root)

        for bad_jsonc in ("{,}", '{"a": [1,,2]}', '{"a": ,}'):
            with self.subTest(bad_jsonc=bad_jsonc):
                with self.assertRaises(json.JSONDecodeError):
                    loads_jsonc(bad_jsonc)

    def test_config_module_imports_stdlib_only(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "config.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertIn(top, sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                self.assertIn(top, sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
