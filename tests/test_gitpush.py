"""Checkpoint publishing: commit + push ``state/`` + ``database/`` every 15 minutes.

The live gap this covers: the agent checkpointed to local ``state/`` every 15
minutes but the git commit happened only once, at the end of the job — so a run
that was killed after five hours published nothing at all.

The tests run against **real git** in a temporary repository with a local bare
"origin", because the parts that actually broke in CI (re-parenting onto the
existing branch tip so the push stays a fast-forward, never staging ``.tmp-*``
files, ignoring "nothing to commit") are git behaviours, not Python ones.
"""


from __future__ import annotations

# The suite must never write the repository's own ``state/`` (LESSONS.md L25):
# imported before any ``agent`` module so ``PYQ_STATE_DIR``/``PYQ_ERRORS_LEDGER``
# are set first, and importable in both discovery modes (``tests.test_x`` with
# ``-t .``, the top-level ``test_x`` without).
try:  # pragma: no cover - the import name depends on the discovery mode
    from tests import _isolation  # noqa: F401
except ImportError:  # pragma: no cover
    import _isolation  # type: ignore[no-redef]  # noqa: F401


import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from agent import gitpush
from agent.gitpush import REASON_DISABLED, REASON_NOTHING, REASON_OK, REASON_THROTTLED, Publisher
from agent.util import Log

QUIET = Log("test", quiet=True)


def git_available() -> bool:
    return shutil.which("git") is not None


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stdout}{proc.stderr}")
    return proc.stdout


class RecordingRunner:
    """Stand-in for ``subprocess.run`` that records the git argv it is given."""

    def __init__(self, responses: Optional[List[Tuple[int, str]]] = None) -> None:
        self.calls: List[List[str]] = []
        self.responses = list(responses or [])

    def __call__(self, args: List[str], *, env: Dict[str, str], cwd: str) -> Tuple[int, str]:
        self.calls.append(list(args))
        if self.responses:
            return self.responses.pop(0)
        return 0, ""


class LogSpy(Log):
    def __init__(self) -> None:
        super().__init__("test", quiet=True)
        self.lines: List[str] = []

    def _emit(self, level: str, message: str) -> None:  # noqa: D102
        self.lines.append(f"{level} {message}")


class EnablePushTests(unittest.TestCase):
    def test_push_is_off_unless_the_environment_enables_it(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(gitpush.enabled())
            result = Publisher(root=Path("."), log=QUIET).publish(phase="phase1")
        self.assertEqual(result.reason, REASON_DISABLED)
        self.assertFalse(result.attempted)

    def test_truthy_values_enable_it(self) -> None:
        for value in ("1", "true", "YES", "on"):
            with mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": value}, clear=True):
                self.assertTrue(gitpush.enabled(), value)

    def test_force_publishes_even_when_disabled(self) -> None:
        runner = RecordingRunner()
        with mock.patch.dict("os.environ", {}, clear=True):
            result = Publisher(root=Path("."), log=QUIET, runner=runner).publish(
                phase="phase1", force=True
            )
        self.assertTrue(result.attempted)
        self.assertEqual(runner.calls[0], ["add", "-f", "state", "database"])

    def test_nothing_to_commit_is_not_an_error(self) -> None:
        runner = RecordingRunner([(0, ""), (0, ""), (0, "")])  # add, diff --cached (empty)
        with mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": "1"}, clear=True):
            result = Publisher(root=Path("."), log=QUIET, runner=runner).publish(phase="phase1")
        self.assertEqual(result.reason, REASON_NOTHING)
        self.assertFalse(result.ok)
        self.assertIn(["add", "-f", "state", "database"], runner.calls)

    def test_temp_files_are_unstaged_before_the_commit(self) -> None:
        runner = RecordingRunner(
            [
                (0, ""),  # git add -f state database
                (0, "state/.tmp-abc.json.part\0state/progress.json\0"),  # git diff --cached -z
                (0, ""),  # git reset -- .tmp-*
                (0, "state/progress.json\0"),  # git diff --cached -z (gate)
                (0, ""),  # git read-tree --empty (prune)
                (0, ""),  # git add -f state database (re-stage)
                (0, ""),  # git rm -r --cached -- database/english/_analysis
                (0, "state/progress.json\0"),  # git diff --cached -z --diff-filter=d (count)
                (0, ""),  # commit
                (0, ""),  # ls-remote
                (0, ""),  # push
            ]
        )
        with mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": "1"}, clear=True):
            result = Publisher(root=Path("."), log=QUIET, runner=runner).publish(
                phase="phase2", done=100, total=200
            )
        self.assertTrue(result.ok)
        self.assertIn(["reset", "-q", "--", "state/.tmp-abc.json.part"], runner.calls)
        commit = next(call for call in runner.calls if "commit" in call)
        self.assertIn("pyq-agent: checkpoint phase2 100/200 [skip ci]", commit)
        self.assertTrue(any("push" in call[0] for call in runner.calls))

    def test_a_token_never_reaches_a_log_line(self) -> None:
        token = "ghp_" + "s3cr3tvalue1234567890"
        spy = LogSpy()
        runner = RecordingRunner(
            [
                (0, ""),  # add -f
                (0, "state/progress.json\0"),  # diff --cached -z (temp-file check)
                (0, "state/progress.json\0"),  # diff --cached -z (gate)
                (0, ""),  # read-tree --empty (prune)
                (0, ""),  # add -f (re-stage)
                (0, ""),  # git rm -r --cached -- database/english/_analysis
                (0, "state/progress.json\0"),  # diff --cached -z --diff-filter=d (count)
                (0, ""),  # commit
                (0, "origin\n"),  # ls-remote --heads origin pyq-db
                (0, ""),  # fetch --depth=1
                (0, ""),  # reset --soft FETCH_HEAD
                (0, ""),  # commit (re-parented)
                (0, "https://github.com/o/r.git\n"),  # remote get-url (token override)
                (0, "remote: https://x-access-token:%s@github.com/o/r.git" % token),  # push output
                (1, "fatal: could not read from remote, token %s" % token),  # push
            ]
        )
        with mock.patch.dict(
            "os.environ", {"PYQ_GIT_PUSH": "1", "PYQ_PUSH_TOKEN": token}, clear=True
        ):
            result = Publisher(
                root=Path("."),
                log=spy,
                remote="origin",
                runner=runner,
            ).publish(phase="phase1")
        # the token url is used for the push, and the token is scrubbed everywhere
        push = next(call for call in runner.calls if call[0] == "push")
        self.assertTrue(any(token in part for part in push), "the override url carries the token")
        self.assertNotIn(token, " ".join(spy.lines))
        self.assertNotIn(token, str(result.reason) + str(result.steps))
        self.assertNotIn(token, json.dumps(result.as_dict()))

    def test_the_derived_analysis_view_is_dropped_from_the_publish(self) -> None:
        """``database/english/_analysis`` must never reach the checkpoint branch.

        It is a derived view: every grammar question in it already has a home
        under ``database/english/grammar/<rule>/``, so publishing it duplicated
        5,318 links in the browsable tree.
        """

        runner = RecordingRunner(
            [
                (0, ""),  # add -f
                (0, "state/progress.json\0"),  # diff --cached -z (temp-file check)
                (0, "state/progress.json\0"),  # diff --cached -z (gate)
                (0, ""),  # read-tree --empty (prune the index to state+database)
                (0, ""),  # add -f (re-stage after the prune)
                (0, ""),  # git rm -r --cached -- database/english/_analysis
                (0, "state/progress.json\0"),  # diff --cached -z --diff-filter=d (count)
            ]
            + [(0, "")] * 8  # commit, ls-remote, fetch, reset --soft, commit, get-url, push...
        )
        with mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": "1"}, clear=True):
            Publisher(root=Path("."), log=QUIET, runner=runner).publish(phase="phase1")
        rm = [call for call in runner.calls if call[0] == "rm"]
        self.assertEqual(len(rm), 1, "exactly one exclusion call")
        self.assertIn("database/english/_analysis", rm[0])
        self.assertIn("--cached", rm[0], "the local files must survive")
        # the exclusion happens before the tree is read back for the commit
        first_commit = next(i for i, call in enumerate(runner.calls) if "commit" in call)
        self.assertLess(runner.calls.index(rm[0]), first_commit)

    def test_maybe_publish_is_throttled_to_the_checkpoint_interval(self) -> None:
        runner = RecordingRunner([(0, ""), (0, ""), (0, "")])
        clock = {"t": 1000.0}
        with mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": "1"}, clear=True), mock.patch.object(
            gitpush, "monotonic", side_effect=lambda: clock["t"]
        ):
            publisher = Publisher(root=Path("."), log=QUIET, runner=runner, interval=900)
            first = publisher.maybe_publish(phase="phase1", done=1, total=10)
            clock["t"] += 60
            second = publisher.maybe_publish(phase="phase1", done=2, total=10)
            clock["t"] += 900
            third = publisher.maybe_publish(phase="phase1", done=3, total=10)
        self.assertEqual(first.reason, REASON_NOTHING)
        self.assertEqual(second.reason, REASON_THROTTLED)
        self.assertIn("next publish in 840s", second.message)
        self.assertEqual(third.reason, REASON_NOTHING, "the interval elapsed again")


@unittest.skipUnless(git_available(), "git is not installed")
class PublisherGitIntegrationTests(unittest.TestCase):
    """The real thing: a temporary repo, a local bare origin, real pushes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / ".gitignore").write_text("state/\ndatabase/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("code only\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore", "README.md")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-u", "origin", "main")

        (self.repo / "state").mkdir()
        (self.repo / "database" / "_meta").mkdir(parents=True)
        (self.repo / "state" / "checkpoint.json").write_text('{"run_id": "r1"}\n', encoding="utf-8")
        (self.repo / "database" / "_meta" / "PROGRESS.md").write_text("# progress\n", encoding="utf-8")

        # NB: the environment is *added to*, not cleared — the real-git tests need
        # PATH to find git (the recorded-runner tests clear it on purpose).
        self.enterContext(mock.patch.dict("os.environ", {"PYQ_GIT_PUSH": "1"}))
        self.log = LogSpy()

    # -- helpers ---------------------------------------------------------
    def publisher(self, **kwargs: Any) -> Publisher:
        return Publisher(root=self.repo, log=self.log, **kwargs)

    def branch_files(self, branch: str = "pyq-db") -> List[str]:
        out = git(self.origin, "ls-tree", "-r", "--name-only", branch)
        return [line for line in out.splitlines() if line]

    def tip(self, branch: str = "pyq-db") -> str:
        return git(self.origin, "rev-parse", branch).strip()

    # -- tests -----------------------------------------------------------
    def test_first_publish_creates_the_branch_and_ignores_gitignore(self) -> None:
        result = self.publisher().publish(phase="phase1", done=20, total=1900)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.committed)
        self.assertTrue(result.pushed)
        self.assertEqual(result.files, 2)
        files = self.branch_files()
        # the branch carries the generated tree and *only* the generated tree:
        # `git commit` writes the whole index, so without the prune the checkout's
        # source files (README.md, .gitignore, agent/, tools/) would be published
        # onto pyq-db as a second, rotting copy of the code
        self.assertEqual(sorted(files), ["database/_meta/PROGRESS.md", "state/checkpoint.json"])
        self.assertNotIn("README.md", files)
        self.assertNotIn(".gitignore", files)
        changed = git(self.origin, "show", "--name-status", "--pretty=format:", "pyq-db")
        self.assertIn("A\tdatabase/_meta/PROGRESS.md", changed)
        self.assertIn("A\tstate/checkpoint.json", changed)
        self.assertIn("D\tREADME.md", changed, "the branch does not carry the code")
        self.assertIn("phase1 20/1,900", git(self.origin, "log", "-1", "--pretty=%s", "pyq-db"))

    def test_a_publish_never_puts_source_files_on_the_branch(self) -> None:
        """``agent/`` must not reappear on the data branch after a re-publish."""

        self.publisher().publish(phase="phase1", done=20, total=1900)
        (self.repo / "state" / "progress.json").write_text('{"phase": "phase2"}\n', encoding="utf-8")
        self.publisher().publish(phase="phase2", done=40, total=1900)
        files = self.branch_files()
        self.assertFalse([name for name in files if name.startswith("agent/")], files)
        self.assertFalse([name for name in files if name.startswith("tools/")], files)
        self.assertEqual(sorted(files), sorted(["database/_meta/PROGRESS.md", "state/checkpoint.json", "state/progress.json"]))

    def test_second_publish_is_a_fast_forward_on_the_existing_tip(self) -> None:
        first = self.publisher().publish(phase="phase1", done=20, total=1900)
        self.assertTrue(first.ok)
        tip = self.tip()
        commits_before = len(git(self.origin, "log", "--oneline", "pyq-db").splitlines())

        (self.repo / "state" / "progress.json").write_text('{"phase": "phase2"}\n', encoding="utf-8")
        second = self.publisher().publish(phase="phase2", done=40, total=1900)

        self.assertTrue(second.ok, second.reason)
        self.assertNotEqual(self.tip(), tip, "the branch advanced")
        # the new commit sits directly on the previous tip: no rewrite, no force
        parents = git(self.origin, "log", "-1", "--pretty=%P", "pyq-db").split()
        self.assertEqual(parents, [tip])
        self.assertEqual(
            len(git(self.origin, "log", "--oneline", "pyq-db").splitlines()),
            commits_before + 1,
        )

    def test_nothing_to_commit_after_the_first_publish(self) -> None:
        self.publisher().publish(phase="phase1", done=20, total=1900)
        again = self.publisher().publish(phase="phase1", done=20, total=1900)
        self.assertEqual(again.reason, REASON_NOTHING)
        self.assertFalse(again.ok)

    def test_temp_files_are_removed_and_never_committed(self) -> None:
        (self.repo / "state" / ".tmp-abc123.json.part").write_text("half written", encoding="utf-8")
        (self.repo / "database" / "_meta" / ".tmp-xyz.md.part").write_text("half", encoding="utf-8")
        (self.repo / "state" / "progress.json").write_text("{}\n", encoding="utf-8")

        result = self.publisher().publish(phase="phase3", done=1, total=2)

        self.assertTrue(result.ok, result.reason)
        self.assertFalse((self.repo / "state" / ".tmp-abc123.json.part").exists())
        files = self.branch_files()
        self.assertFalse([f for f in files if ".tmp-" in f or f.endswith(".part")], files)
        self.assertIn("state/progress.json", files)

    def test_a_failed_push_is_not_fatal_and_does_not_stop_the_run(self) -> None:
        # an unreachable remote: the publish fails, the run continues
        (self.repo / "state" / "progress.json").write_text("{}\n", encoding="utf-8")

        result = self.publisher(remote="no-such-remote").publish(phase="phase1", done=1, total=2)

        self.assertTrue(result.attempted)
        self.assertFalse(result.ok)
        self.assertFalse(result.pushed)
        self.assertTrue(result.committed, "the local commit still happened")
        self.assertIn("push", result.reason)
        self.assertTrue(any("WARN" in line for line in self.log.lines))

    def test_a_missing_repository_is_not_fatal(self) -> None:
        empty = self.tmp / "not-a-repo"
        empty.mkdir()
        (empty / "state").mkdir()
        result = Publisher(root=empty, log=self.log).publish(phase="phase1", done=1, total=2)
        self.assertTrue(result.attempted)
        self.assertFalse(result.ok)
        self.assertIn("git add failed", result.reason)

    def test_maybe_publish_uses_the_checkpoint_interval_from_the_environment(self) -> None:
        (self.repo / "state" / "progress.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"CHECKPOINT_INTERVAL_SECONDS": "900"}):
            publisher = Publisher(root=self.repo, log=self.log)
            self.assertEqual(publisher.interval, 900.0)
            first = publisher.maybe_publish(phase="phase1", done=1, total=2)
            second = publisher.maybe_publish(phase="phase1", done=2, total=2)
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(second.reason, REASON_THROTTLED)


if __name__ == "__main__":
    unittest.main()
