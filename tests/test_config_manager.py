import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.task_state_fixtures import make_task_state


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "configure"
    / "config_manager.py"
)
SPEC = importlib.util.spec_from_file_location("config_manager", MODULE_PATH)
assert SPEC and SPEC.loader
config_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_manager)
import run_guardian


class ConfigManagerTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, Path]:
        vault = root / "vault"
        config_path = vault / ".dailypaper" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "paths": {"obsidian_vault": "."},
                    "repository": {
                        "url": "git@github.com:haoz0206/dailypaper-vault.git",
                        "remote": "origin",
                        "branch": "main",
                    },
                }
            ),
            encoding="utf-8",
        )
        return vault, config_path

    def _publication_vault(
        self,
        root: Path,
        *,
        task_state: dict | None = None,
    ) -> tuple[Path, Path, Path]:
        remote = root / "remote.git"
        seed = root / "seed"
        vault = root / "vault"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(seed)],
            check=True,
            capture_output=True,
            text=True,
        )
        config_path = seed / ".dailypaper" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "paths": {"obsidian_vault": "."},
                    "repository": {
                        "url": str(remote),
                        "remote": "origin",
                        "branch": "main",
                    },
                }
            ),
            encoding="utf-8",
        )
        if task_state is not None:
            state_path = seed / ".dailypaper" / "tasks" / "daily-papers.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps(task_state), encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(seed), "add", "."],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(seed),
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(seed), "push", "-u", "origin", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clone", str(remote), str(vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        return remote, vault, vault / ".dailypaper" / "config.json"

    @contextmanager
    def _repository_contract(self, remote: Path):
        defaults = copy.deepcopy(config_manager.DEFAULT_CONFIG)
        defaults["repository"]["url"] = str(remote)
        base = json.loads(
            (
                REPO_ROOT
                / "skills"
                / "daily-papers"
                / "scripts"
                / "shared"
                / "user-config.json"
            ).read_text(encoding="utf-8")
        )
        base["repository"]["url"] = str(remote)
        with (
            patch.object(config_manager, "DEFAULT_CONFIG", defaults),
            patch.object(
                config_manager,
                "_base_config",
                side_effect=lambda: copy.deepcopy(base),
            ),
        ):
            yield

    def test_plan_normalizes_keywords_and_pins_daily_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "keywords": [
                                " Robot Learning ",
                                "robot learning",
                                "VLA",
                            ],
                            "top_n": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            plan = config_manager.build_plan(config_path, patch_path)

            self.assertEqual(
                plan["proposed"]["daily_papers"]["keywords"],
                ["robot learning", "vla"],
            )
            self.assertEqual(plan["proposed"]["daily_papers"]["top_n"], 15)
            self.assertEqual(
                plan["proposed"]["daily_papers"]["arxiv_categories"],
                ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
            )
            self.assertEqual(
                plan["proposed"]["repository"]["branch"],
                "main",
            )

    def test_apply_writes_valid_config_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "arxiv_categories": ["cs.RO", "cs.CV"],
                            "min_score": 3,
                        },
                        "automation": {"auto_refresh_indexes": False},
                    }
                ),
                encoding="utf-8",
            )

            with self._repository_contract(remote):
                result = config_manager.apply_plan(
                    vault,
                    config_path,
                    patch_path,
                )
                repeated = config_manager.apply_plan(
                    vault,
                    config_path,
                    patch_path,
                )
                validated = config_manager.validate_config(config_path)
            written = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "published")
            self.assertEqual(repeated["status"], "already-published")
            self.assertEqual(
                written["daily_papers"]["arxiv_categories"],
                ["cs.RO", "cs.CV"],
            )
            self.assertEqual(written["daily_papers"]["min_score"], 3)
            self.assertFalse(written["automation"]["auto_refresh_indexes"])
            self.assertEqual(
                validated["daily_papers"]["min_score"],
                3,
            )
            self.assertFalse(list(config_path.parent.glob(".config.*.tmp")))
            self.assertEqual(result["remote_head"], result["commit"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                result["commit"],
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(vault), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )

            other_vault = root / "other-vault"
            subprocess.run(
                ["git", "clone", str(remote), str(other_vault)],
                check=True,
                capture_output=True,
                text=True,
            )
            with self._repository_contract(remote):
                cross_machine_repeat = config_manager.apply_plan(
                    other_vault,
                    other_vault / ".dailypaper" / "config.json",
                    patch_path,
                )
            self.assertEqual(cross_machine_repeat["status"], "unchanged")
            self.assertIsNone(cross_machine_repeat["commit"])

            next_patch = root / "next-patch.json"
            next_patch.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            with self._repository_contract(remote):
                next_result = config_manager.apply_plan(
                    vault,
                    config_path,
                    next_patch,
                )
            self.assertEqual(next_result["status"], "published")
            self.assertNotEqual(next_result["commit"], result["commit"])

    def test_apply_does_not_follow_symlinked_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            target = root / "target-patch.json"
            target.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            expected = target.read_bytes()
            patch_path = root / "patch.json"
            patch_path.symlink_to(target)

            with (
                self._repository_contract(remote),
                self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "readable regular file",
                ),
            ):
                config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(target.read_bytes(), expected)

    def test_prepare_fast_forwards_a_clean_configuration_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, _config_path = self._publication_vault(root)
            other = root / "other"
            subprocess.run(
                ["git", "clone", str(remote), str(other)],
                check=True,
                capture_output=True,
                text=True,
            )
            (other / "README.md").write_text("new\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(other), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(other),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "advance",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(other), "push", "origin", "main"],
                check=True,
                capture_output=True,
                text=True,
            )

            with self._repository_contract(remote):
                result = config_manager.prepare_configuration(vault)

            self.assertTrue(result["pulled"])
            self.assertEqual(result["local_head"], result["remote_head"])

    def test_apply_reuses_preserved_commit_after_push_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            base = subprocess.run(
                ["git", "-C", str(vault), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            real_git = config_manager._git

            def reject_push(vault_path: Path, *args: str, check: bool = True):
                if args and args[0] == "push":
                    return subprocess.CompletedProcess(
                        ["git", *args],
                        1,
                        "",
                        "simulated network failure",
                    )
                return real_git(vault_path, *args, check=check)

            with (
                self._repository_contract(remote),
                patch.object(config_manager, "_git", side_effect=reject_push),
            ):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "preserved locally",
                ):
                    config_manager.apply_plan(vault, config_path, patch_path)
            preserved = subprocess.run(
                ["git", "-C", str(vault), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(preserved, base)

            with self._repository_contract(remote):
                result = config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["commit"], preserved)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                preserved,
            )

    def test_apply_accepts_successful_push_with_lost_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            real_git = config_manager._git

            def ambiguous_push(vault_path: Path, *args: str, check: bool = True):
                result = real_git(vault_path, *args, check=check)
                if args and args[0] == "push":
                    return subprocess.CompletedProcess(
                        result.args,
                        1,
                        result.stdout,
                        "response lost",
                    )
                return result

            with (
                self._repository_contract(remote),
                patch.object(config_manager, "_git", side_effect=ambiguous_push),
            ):
                result = config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["remote_head"], result["commit"])

    def test_apply_respects_shared_vault_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            original = config_path.read_bytes()
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )

            with (
                self._repository_contract(remote),
                run_guardian.hold_vault_writer_lock(vault),
            ):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "already owns",
                ):
                    config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(config_path.read_bytes(), original)

    def test_apply_recovers_from_every_persisted_transaction_phase(self) -> None:
        failpoints = (
            "after-transaction",
            "after-config-write",
            "after-commit",
            "after-local-update",
            "after-push",
        )
        for failpoint in failpoints:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                remote, vault, config_path = self._publication_vault(root)
                patch_path = root / "patch.json"
                patch_path.write_text(
                    json.dumps({"daily_papers": {"top_n": 12}}),
                    encoding="utf-8",
                )

                def inject(name: str) -> None:
                    if name == failpoint:
                        raise config_manager.ConfigError(
                            f"simulated crash at {name}"
                        )

                with (
                    self._repository_contract(remote),
                    patch.object(
                        config_manager,
                        "_configuration_failpoint",
                        side_effect=inject,
                    ),
                ):
                    with self.assertRaisesRegex(
                        config_manager.ConfigError,
                        "simulated crash",
                    ):
                        config_manager.apply_plan(
                            vault,
                            config_path,
                            patch_path,
                        )

                with self._repository_contract(remote):
                    recovered = config_manager.apply_plan(
                        vault,
                        config_path,
                        patch_path,
                    )
                    effective = config_manager.validate_config(config_path)

                self.assertIn(
                    recovered["status"],
                    {"published", "already-published"},
                )
                self.assertEqual(effective["daily_papers"]["top_n"], 12)
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(vault), "status", "--porcelain"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                    "",
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(vault), "rev-parse", "HEAD"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(remote),
                            "rev-parse",
                            "refs/heads/main",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                )

    def test_pending_transaction_cannot_be_preempted_by_another_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            original_patch = root / "original.json"
            original_patch.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            other_patch = root / "other.json"
            other_patch.write_text(
                json.dumps({"daily_papers": {"top_n": 18}}),
                encoding="utf-8",
            )

            def interrupt(name: str) -> None:
                if name == "after-transaction":
                    raise config_manager.ConfigError("simulated interruption")

            with (
                self._repository_contract(remote),
                patch.object(
                    config_manager,
                    "_configuration_failpoint",
                    side_effect=interrupt,
                ),
            ):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "simulated interruption",
                ):
                    config_manager.apply_plan(
                        vault,
                        config_path,
                        original_patch,
                    )

            with self._repository_contract(remote):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "original patch",
                ):
                    config_manager.apply_plan(vault, config_path, other_patch)
                recovered = config_manager.apply_plan(
                    vault,
                    config_path,
                    original_patch,
                )

            self.assertEqual(recovered["status"], "published")
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))[
                    "daily_papers"
                ]["top_n"],
                12,
            )

    def test_resume_does_not_require_the_temporary_patch_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )

            def interrupt(name: str) -> None:
                if name == "after-config-write":
                    raise config_manager.ConfigError("simulated interruption")

            with (
                self._repository_contract(remote),
                patch.object(
                    config_manager,
                    "_configuration_failpoint",
                    side_effect=interrupt,
                ),
            ):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "simulated interruption",
                ):
                    config_manager.apply_plan(vault, config_path, patch_path)
            patch_path.unlink()

            with self._repository_contract(remote):
                recovered = config_manager.resume_publication(vault, config_path)
                repeated = config_manager.resume_publication(vault, config_path)

            self.assertEqual(recovered["status"], "published")
            self.assertEqual(repeated["status"], "already-published")
            self.assertEqual(recovered["commit"], repeated["commit"])

    def test_resume_preserves_an_unregistered_staged_config_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            before = config_path.read_bytes()
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )

            def interrupt(name: str) -> None:
                if name == "after-transaction":
                    raise config_manager.ConfigError("simulated interruption")

            with (
                self._repository_contract(remote),
                patch.object(
                    config_manager,
                    "_configuration_failpoint",
                    side_effect=interrupt,
                ),
            ):
                with self.assertRaises(config_manager.ConfigError):
                    config_manager.apply_plan(vault, config_path, patch_path)

            staged = json.loads(before)
            staged["daily_papers"] = {"top_n": 99}
            staged_bytes = (json.dumps(staged) + "\n").encode("utf-8")
            config_path.write_bytes(staged_bytes)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "add",
                    "--",
                    ".dailypaper/config.json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            config_path.write_bytes(before)

            with self._repository_contract(remote):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "unregistered configuration version",
                ):
                    config_manager.resume_publication(vault, config_path)

            self.assertEqual(
                config_manager._git_blob(
                    vault,
                    ":.dailypaper/config.json",
                ),
                staged_bytes,
            )
            self.assertEqual(config_path.read_bytes(), before)

    def test_symlinked_transaction_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            outside = root / "outside.json"
            outside.write_text("preserve\n", encoding="utf-8")
            transaction = config_manager._transaction_path(vault)
            transaction.parent.mkdir(parents=True, exist_ok=True)
            transaction.symlink_to(outside)

            with self._repository_contract(remote):
                with self.assertRaisesRegex(
                    config_manager.ConfigError,
                    "must not be a symlink",
                ):
                    config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve\n")

    def test_apply_fresh_checks_remote_ownership_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(root)
            original = config_path.read_bytes()
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            with (
                self._repository_contract(remote),
                patch.object(
                    config_manager,
                    "_fresh_remote_state",
                    side_effect=config_manager.ActiveRunError(
                        "remote run owns the Vault"
                    ),
                ),
            ):
                with self.assertRaisesRegex(config_manager.ActiveRunError, "remote run"):
                    config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(config_path.read_bytes(), original)

    def test_plan_exposes_standalone_git_automation_without_affecting_daily(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "automation": {
                            "git_commit": True,
                            "git_push": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            plan = config_manager.build_plan(config_path, patch_path)

            self.assertTrue(plan["proposed"]["automation"]["git_commit"])
            self.assertTrue(plan["proposed"]["automation"]["git_push"])
            changed = {change["path"] for change in plan["changes"]}
            self.assertEqual(
                changed,
                {
                    "automation.git_commit",
                    "automation.git_push",
                },
            )

    def test_rejects_unsupported_inert_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "arxiv_date_mode": "calendar-day",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "Unsupported daily_papers patch fields",
            ):
                config_manager.build_plan(config_path, patch_path)

    def test_rejects_positive_negative_keyword_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "keywords": ["robot learning"],
                            "negative_keywords": ["robot learning"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "conflict",
            ):
                config_manager.build_plan(config_path, patch_path)

    def test_active_run_blocks_apply_without_changing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, vault, config_path = self._publication_vault(
                root,
                task_state=make_task_state(
                    "running",
                    run_id="run-apply-race",
                    harness="claude-code",
                    owner="server",
                ),
            )
            original = config_path.read_bytes()
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )

            with self._repository_contract(remote):
                with self.assertRaises(config_manager.ActiveRunError):
                    config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse(list(config_path.parent.glob(".config.*.tmp")))

    def test_shared_vault_path_cannot_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["obsidian_vault"] = "/workspace/private-vault"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "must remain",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_unknown_or_secret_shared_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["credentials"] = {"github_token": "secret"}
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "unsupported sections",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_per_machine_zotero_paths_in_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["zotero_db"] = "/srv/zotero.sqlite"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "unsupported paths fields",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_unsafe_relative_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["paper_notes_folder"] = "../elsewhere"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "safe relative POSIX path",
            ):
                config_manager.validate_config(config_path)

    def test_shared_config_parent_cannot_escape_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            outside = root / "outside"
            vault.mkdir()
            outside.mkdir()
            (vault / ".dailypaper").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "must not be a symlink",
            ):
                config_manager.resolve_config_path(vault)


if __name__ == "__main__":
    unittest.main()
