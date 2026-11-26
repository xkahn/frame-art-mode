from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "frame_art_mode"
TESTS_DIR = REPO_ROOT / "tests"

LEGACY_MODULES = frozenset({"main", "service", "web"})
ALL_THIRD_PARTY_OR_INTERNAL_FORBIDDEN = frozenset(
    {
        "flask",
        "werkzeug",
        "jinja2",
        "PIL",
        "Pillow",
        "samsungframetv",
        "unidecode",
        "websocket",
    }
)


def _parse_module(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _collect_ast_imports(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return (external_top_levels, internal_submodules) imported anywhere in tree."""
    external: set[str] = set()
    internal: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "frame_art_mode":
                    if len(parts) > 1:
                        internal.add(parts[1])
                else:
                    external.add(parts[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                if node.module:
                    internal.add(node.module.split(".")[0])
                else:
                    for alias in node.names:
                        internal.add(alias.name.split(".")[0])
            elif node.module:
                parts = node.module.split(".")
                if parts[0] == "frame_art_mode":
                    if len(parts) > 1:
                        internal.add(parts[1])
                else:
                    external.add(parts[0])
    return external, internal


def _collect_top_level_imports(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _fresh_sys_modules(module_name: str) -> set[str]:
    script = (
        "import importlib, json, sys; "
        f"importlib.import_module({module_name!r}); "
        "print(json.dumps(sorted(sys.modules.keys())))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(proc.stdout))


def _internal_submodules_loaded(loaded: set[str]) -> set[str]:
    return {
        name.split(".")[1]
        for name in loaded
        if name.startswith("frame_art_mode.") and len(name.split(".")) >= 2
    }


def _top_level_modules_loaded(loaded: set[str]) -> set[str]:
    return {name.split(".")[0] for name in loaded}


class ImportBoundaryTests(unittest.TestCase):
    def test_shared_modules_are_stdlib_only_ast_and_runtime(self):
        cases = {
            "config": set(),
            "models": set(),
            "ipc": {"models"},
        }
        for mod_name, allowed_internal in cases.items():
            with self.subTest(module=mod_name):
                _, tree = _parse_module(PACKAGE_DIR / f"{mod_name}.py")
                external, internal = _collect_ast_imports(tree)
                for ext in external:
                    self.assertIn(ext, sys.stdlib_module_names)
                self.assertTrue(
                    internal <= allowed_internal,
                    f"{mod_name}.py imports disallowed internal modules: {internal - allowed_internal}",
                )

                loaded = _fresh_sys_modules(f"frame_art_mode.{mod_name}")
                loaded_internal = _internal_submodules_loaded(loaded)
                self.assertEqual(loaded_internal, {mod_name} | allowed_internal)
                loaded_top = _top_level_modules_loaded(loaded)
                self.assertFalse(loaded_top & ALL_THIRD_PARTY_OR_INTERNAL_FORBIDDEN)

    def test_supervisor_boundary_ast_and_runtime(self):
        source, tree = _parse_module(PACKAGE_DIR / "supervisor.py")
        external, internal = _collect_ast_imports(tree)
        for ext in external:
            self.assertIn(ext, sys.stdlib_module_names)
        self.assertLessEqual(internal, {"config"})

        for forbidden_token in ("tv.sock", "library.sock", "tv.lock", "library.lock"):
            self.assertNotIn(forbidden_token, source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "image_root":
                self.assertIsInstance(node.value, ast.Name)
                self.assertEqual(node.value.id, "args")

        loaded = _fresh_sys_modules("frame_art_mode.supervisor")
        self.assertEqual(_internal_submodules_loaded(loaded), {"supervisor", "config"})
        loaded_top = _top_level_modules_loaded(loaded)
        self.assertFalse(loaded_top & ALL_THIRD_PARTY_OR_INTERNAL_FORBIDDEN)

    def test_web_server_boundary_ast_and_runtime(self):
        _, tree = _parse_module(PACKAGE_DIR / "web_server.py")
        external, internal = _collect_ast_imports(tree)
        forbidden_ext = {"samsungframetv", "PIL", "Pillow", "unidecode"}
        forbidden_int = {"tv_daemon", "library_daemon", "images"} | LEGACY_MODULES
        self.assertFalse(external & forbidden_ext)
        self.assertFalse(internal & forbidden_int)
        self.assertLessEqual(internal, {"config", "ipc", "models"})

        # Only CLI arg forwarding in main() may read args.image_root; never config.image_root.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "image_root":
                self.assertIsInstance(node.value, ast.Name)
                self.assertEqual(node.value.id, "args")

        loaded = _fresh_sys_modules("frame_art_mode.web_server")
        loaded_top = _top_level_modules_loaded(loaded)
        loaded_internal = _internal_submodules_loaded(loaded)
        self.assertFalse(loaded_top & forbidden_ext)
        self.assertFalse(loaded_internal & forbidden_int)

    def test_tv_daemon_boundary_ast_and_runtime(self):
        source, tree = _parse_module(PACKAGE_DIR / "tv_daemon.py")
        top_level = _collect_top_level_imports(tree)
        self.assertNotIn("samsungframetv", top_level)

        external, internal = _collect_ast_imports(tree)
        forbidden_ext = {"PIL", "Pillow", "flask", "unidecode"}
        forbidden_int = {"images", "library_daemon", "web_server"} | LEGACY_MODULES
        self.assertFalse(external & forbidden_ext)
        self.assertFalse(internal & forbidden_int)
        self.assertNotIn("image_root", source)

        loaded = _fresh_sys_modules("frame_art_mode.tv_daemon")
        loaded_top = _top_level_modules_loaded(loaded)
        loaded_internal = _internal_submodules_loaded(loaded)
        self.assertFalse(loaded_top & (forbidden_ext | {"samsungframetv"}))
        self.assertFalse(loaded_internal & forbidden_int)

    def test_library_daemon_and_images_boundaries_ast_and_runtime(self):
        forbidden_ext = {"samsungframetv", "flask"}
        forbidden_int = {"tv_daemon", "web_server"} | LEGACY_MODULES

        for mod_name in ("library_daemon", "images"):
            with self.subTest(module=mod_name):
                _, tree = _parse_module(PACKAGE_DIR / f"{mod_name}.py")
                external, internal = _collect_ast_imports(tree)
                self.assertFalse(external & forbidden_ext)
                self.assertFalse(internal & forbidden_int)

                loaded = _fresh_sys_modules(f"frame_art_mode.{mod_name}")
                loaded_top = _top_level_modules_loaded(loaded)
                loaded_internal = _internal_submodules_loaded(loaded)
                self.assertFalse(loaded_top & forbidden_ext)
                self.assertFalse(loaded_internal & forbidden_int)

    def test_legacy_monolith_files_and_imports_are_removed(self):
        for legacy_path in (
            PACKAGE_DIR / "main.py",
            PACKAGE_DIR / "service.py",
            PACKAGE_DIR / "web.py",
            TESTS_DIR / "test_service.py",
        ):
            self.assertFalse(legacy_path.exists(), f"Legacy file still exists: {legacy_path}")

        for py_file in list(PACKAGE_DIR.glob("*.py")) + list(TESTS_DIR.glob("*.py")):
            _, tree = _parse_module(py_file)
            _external, internal = _collect_ast_imports(tree)
            self.assertFalse(
                internal & LEGACY_MODULES,
                f"{py_file} still imports legacy module(s): {internal & LEGACY_MODULES}",
            )


if __name__ == "__main__":
    unittest.main()
