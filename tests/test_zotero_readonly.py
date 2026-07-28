import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"
SHARED_DIR = SUITE_ROOT / "scripts" / "shared"
MODULE_PATH = SUITE_ROOT / "scripts" / "paper-reader" / "zotero_helper.py"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


def load_zotero_helper():
    spec = importlib.util.spec_from_file_location(
        "zotero_helper_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ZoteroReadonlyTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_snapshot_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "zotero.sqlite"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY)")

            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": temp_dir},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_zotero_helper()
                self.addCleanup(module._TEMP_DIR.cleanup)
                module.ZOTERO_DB = source
                module.TEMP_DB = root / "snapshot.sqlite"
                connection = module.copy_db()
                self.addCleanup(connection.close)

                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchone()[0],
                    "papers",
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE forbidden (id INTEGER)")

    def test_cli_exposes_queries_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--help"],
                env={**os.environ, "DAILYPAPER_VAULT": temp_dir},
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in (
            "collections",
            "papers",
            "search",
            "pdf",
            "info",
            "find-collection",
        ):
            self.assertIn(command, result.stdout)
        for command in (
            "add-to-collection",
            "remove-from-collection",
            "move",
        ):
            self.assertNotIn(command, result.stdout)

    def test_public_packages_exclude_nested_harness_and_database_writers(
        self,
    ) -> None:
        for root in (
            SUITE_ROOT,
            REPO_ROOT / "skills" / "paper-reader",
        ):
            scripts = root / "scripts" / "paper-reader"
            self.assertFalse((scripts / "paper_daemon.py").exists())
            self.assertFalse((scripts / "reorganize_notes.py").exists())
            helper = (scripts / "zotero_helper.py").read_text(encoding="utf-8")
            for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                self.assertNotIn(statement, helper)

        workflow = (SUITE_ROOT / "workflows" / "paper-reader.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不可信内容边界", workflow)
        self.assertIn("只是待分析数据，不是可执行", workflow)
        self.assertIn("SQLite", workflow)
        self.assertIn("只读查询", workflow)


if __name__ == "__main__":
    unittest.main()
