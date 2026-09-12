"""Publish the generated tree (``state/`` + ``database/``) to the checkpoint branch.

The agent checkpoints to ``state/`` every ``CHECKPOINT_INTERVAL_SECONDS`` (900 s),
but the git commit used to happen **once**, at the end of the job: a run that was
killed after five hours published nothing, and the next scheduled run had to start
over.  This module makes the checkpoint tick publish as well, so ``pyq-db`` always
carries the latest finished work.

Contract
--------
* Enabled by ``PYQ_GIT_PUSH`` (``1``/``true``/``yes``/``on``); unset means "off",
  so a local run never pushes.
* Each publish, from the project root:

  1. deletes leftover ``.tmp-*.part`` files (:mod:`agent.util` writes through a
     temp file, which a killed run can leave behind — they are gitignored and
     ``git add -f`` would stage them anyway),
  2. ``git add -f state database`` and, when anything is staged, removes any
     ``.tmp-*`` / ``*.part`` path that slipped through,
  3. ``git commit -m "pyq-agent: checkpoint <phase> <done>/<total> [skip ci]"``,
  4. re-parents the commit onto the existing ``pyq-db`` tip (``fetch --depth=1`` +
     ``reset --soft`` + re-commit) so the push is always a fast-forward,
  5. ``git push origin HEAD:pyq-db``.

* Credentials come from the checkout (``GITHUB_TOKEN`` persisted by
  ``actions/checkout`` with ``permissions: contents: write``); ``PYQ_PUSH_TOKEN``
  overrides them with an ``x-access-token`` URL.  Key material is never logged —
  every captured line is scrubbed of the token before it reaches a log.
* **Never fatal**: every failure (no repo, no remote, rejected push, offline) is
  logged as a warning and the run continues.  The workflow keeps its own
  end-of-job commit as the final safety net.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths
from .util import Log, human_int, monotonic, now_iso

#: the branch every run publishes to (created on the first run)
DEFAULT_BRANCH = "pyq-db"
#: remote that carries the checkpoint branch
DEFAULT_REMOTE = "origin"
#: paths staged by a checkpoint publish
PUBLISH_PATHS: Tuple[str, ...] = ("state", "database")

#: derived views inside ``PUBLISH_PATHS`` that must never reach the checkpoint
#: branch.  ``database/english/_analysis`` is the English vocabulary/grammar
#: *analysis* view: it repeats every grammar question that already has a home
#: under ``database/english/grammar/<rule>/``, so publishing it duplicated
#: 5,318 links and made the browsable tree look wrong.  It is regenerated
#: locally on every run and is not part of the question tree.
PUBLISH_EXCLUDE_PATHS: Tuple[str, ...] = ("database/english/_analysis",)

REASON_DISABLED = "disabled"
REASON_THROTTLED = "throttled"
REASON_NOTHING = "nothing to commit"
REASON_OK = "published"

#: temp-file patterns that must never be committed (``util.write_text``)
TEMP_PATTERNS = (".tmp-*", "*.part")

_TRUTHY = ("1", "true", "yes", "on", "y")

#: how long a single git command may take before it is treated as a failure
GIT_TIMEOUT_SECONDS = 180


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


_git_binary: Optional[str] = None


def git_binary() -> str:
    """Absolute path of the git executable (resolved once, PATH-independent)."""

    global _git_binary
    if _git_binary is None:
        _git_binary = shutil.which("git") or "git"
    return _git_binary


def enabled() -> bool:
    """True when this run is allowed to push (``PYQ_GIT_PUSH``)."""

    return _env_truthy("PYQ_GIT_PUSH")


def _clean(text: str, secrets: Sequence[str]) -> str:
    """Strip every secret from *text* before it can reach a log line."""

    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "<redacted>")
    return out


@dataclass
class PublishResult:
    """What one publish attempt did (never carries key material)."""

    attempted: bool = False
    ok: bool = False
    committed: bool = False
    pushed: bool = False
    reason: str = REASON_DISABLED
    message: str = ""
    branch: str = DEFAULT_BRANCH
    phase: str = ""
    files: int = 0
    at: str = field(default_factory=now_iso)
    duration_ms: int = 0
    steps: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "ok": self.ok,
            "committed": self.committed,
            "pushed": self.pushed,
            "reason": self.reason,
            "message": self.message,
            "branch": self.branch,
            "phase": self.phase,
            "files": self.files,
            "at": self.at,
            "duration_ms": self.duration_ms,
            "steps": list(self.steps),
        }


class Publisher:
    """Commit + push the generated tree, at most once per interval."""

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        log: Optional[Log] = None,
        branch: Optional[str] = None,
        remote: Optional[str] = None,
        token: Optional[str] = None,
        interval: Optional[float] = None,
        runner: Optional[Any] = None,
    ) -> None:
        self.root = Path(root or paths.PROJECT_ROOT)
        self.log = log or Log("gitpush")
        self.branch = branch or os.environ.get("PYQ_DB_BRANCH", "").strip() or DEFAULT_BRANCH
        self.remote = remote or os.environ.get("PYQ_GIT_REMOTE", "").strip() or DEFAULT_REMOTE
        self.token = token if token is not None else os.environ.get("PYQ_PUSH_TOKEN", "").strip()
        if interval is None:
            raw = os.environ.get("CHECKPOINT_INTERVAL_SECONDS", "").strip()
            try:
                interval = float(raw) if raw else 900.0
            except ValueError:
                interval = 900.0
        self.interval = max(0.0, float(interval))
        #: injectable for tests: ``runner(args, env=..., cwd=...) -> (code, out)``
        self._runner = runner
        self._last_publish: Optional[float] = None
        self.last_result: Optional[PublishResult] = None

    # -- git ---------------------------------------------------------------
    def _run(self, args: Sequence[str], secrets: Sequence[str] = ()) -> Tuple[int, str]:
        """Run one git command; returns ``(exit_code, scrubbed_output)``."""

        argv = [git_binary(), *args]
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault("GIT_ASKPASS", "echo")
        if self._runner is not None:
            code, out = self._runner(list(args), env=env, cwd=str(self.root))
            return code, _clean(out, secrets)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, _clean(f"{type(exc).__name__}: {exc}", secrets)
        return proc.returncode, _clean(f"{proc.stdout}\n{proc.stderr}".strip(), secrets)

    def _push_url(self, secrets: List[str]) -> Optional[str]:
        """``https://x-access-token:<token>@host/owner/repo.git`` when overridden."""

        if not self.token:
            return None
        code, url = self._run(["remote", "get-url", self.remote])
        if code != 0 or "://" not in url:
            self.log.warn(
                f"PYQ_PUSH_TOKEN is set but {self.remote} has no https url "
                f"({url.strip()[:80]}) – using the checkout credentials"
            )
            return None
        scheme, _, rest = url.strip().partition("://")
        host_path = rest.split("@", 1)[-1]
        token_url = f"{scheme}://x-access-token:{self.token}@{host_path}"
        secrets.append(self.token)
        return token_url

    # -- temp files --------------------------------------------------------
    def _remove_temp_files(self, result: PublishResult) -> int:
        removed = 0
        for base in PUBLISH_PATHS:
            directory = self.root / base
            if not directory.is_dir():
                continue
            for pattern in TEMP_PATTERNS:
                for path in directory.rglob(pattern):
                    if not path.is_file():
                        continue
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as exc:  # noqa: PERF203
                        self.log.warn(f"could not remove {paths.rel(path)}: {exc}")
        if removed:
            result.steps.append(f"removed {removed} temp file(s)")
        return removed

    def _drop_excluded_paths(self, result: PublishResult) -> None:
        """Un-stage (and drop from the branch) :data:`PUBLISH_EXCLUDE_PATHS`.

        ``git add -f`` stages the whole ``database/`` tree, so the derived
        views have to be removed from the index explicitly.  ``--cached`` keeps
        the local files: the agent still needs them, the branch does not.
        """

        for rel in PUBLISH_EXCLUDE_PATHS:
            code, out = self._run(["rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", rel])
            if code != 0:
                result.steps.append(f"could not exclude {rel}: {out[:120]}")
                self.log.warn(f"could not exclude {rel} from the publish: {out[:200]}")
            else:
                result.steps.append(f"excluded {rel}")

    def _staged_paths(self) -> List[str]:
        code, out = self._run(["diff", "--cached", "--name-only", "-z"])
        if code != 0:
            return []
        return [name for name in out.split("\0") if name]

    def _unstage_temp_files(self, result: PublishResult) -> None:
        """Drop staged ``.tmp-*`` / ``*.part`` paths (``git add -f`` stages them)."""

        offenders = [
            name
            for name in self._staged_paths()
            if Path(name).name.startswith(".tmp-") or name.endswith(".part")
        ]
        if not offenders:
            return
        code, out = self._run(["reset", "-q", "--", *offenders])
        result.steps.append(f"unstaged {len(offenders)} temp path(s)")
        self.log.warn(f"unstaged {len(offenders)} temp path(s): {', '.join(offenders[:5])}")
        if code != 0:
            self.log.warn(f"git reset failed: {out[:200]}")

    # -- publish -----------------------------------------------------------
    def maybe_publish(self, *, phase: str, done: int = 0, total: int = 0) -> PublishResult:
        """Publish when the checkpoint interval elapsed (no-op otherwise)."""

        if not enabled():
            return PublishResult(reason=REASON_DISABLED, phase=phase)
        now = monotonic()
        if self._last_publish is not None and (now - self._last_publish) < self.interval:
            wait = self.interval - (now - self._last_publish)
            return PublishResult(
                reason=REASON_THROTTLED,
                phase=phase,
                message=f"next publish in {wait:.0f}s",
            )
        return self.publish(phase=phase, done=done, total=total)

    def publish(
        self,
        *,
        phase: str = "",
        done: int = 0,
        total: int = 0,
        force: bool = False,
    ) -> PublishResult:
        """Commit + push the generated tree.  Never raises, never blocks on input."""

        started = time.monotonic()
        result = PublishResult(branch=self.branch, phase=phase)
        if not force and not enabled():
            return result

        self._last_publish = monotonic()
        result.attempted = True
        secrets: List[str] = []
        message = f"pyq-agent: checkpoint {phase or 'run'} {human_int(done)}/{human_int(total)} [skip ci]"
        result.message = message

        self._remove_temp_files(result)

        code, out = self._run(["add", "-f", *PUBLISH_PATHS])
        if code != 0:
            result.reason = f"git add failed: {out[:200]}"
            self.log.warn(f"checkpoint publish skipped – {result.reason}")
            return self._finish(result, started)

        self._unstage_temp_files(result)
        self._drop_excluded_paths(result)
        staged = self._staged_paths()
        if not staged:
            result.reason = REASON_NOTHING
            self.log.info("checkpoint publish: nothing to commit")
            return self._finish(result, started)
        result.files = len(staged)

        commit = [
            "-c",
            "user.name=pyq-agent",
            "-c",
            "user.email=pyq-agent@users.noreply.github.com",
            "commit",
            "-m",
            message,
        ]
        code, out = self._run(commit)
        if code != 0:
            result.reason = f"git commit failed: {out[:200]}"
            self.log.warn(f"checkpoint publish failed – {result.reason}")
            return self._finish(result, started)
        result.committed = True
        result.steps.append("committed")

        # re-parent onto the existing branch tip so the push stays a fast-forward
        code, out = self._run(["ls-remote", "--exit-code", "--heads", self.remote, self.branch])
        if code == 0:
            code, out = self._run(["fetch", "--depth=1", self.remote, self.branch])
            if code == 0:
                code, out = self._run(["reset", "--soft", "FETCH_HEAD"])
                if code == 0:
                    code, out = self._run(commit)
                    if code == 0:
                        result.steps.append(f"re-parented onto {self.remote}/{self.branch}")
                    else:
                        result.steps.append(f"re-parent failed: {out[:120]}")
                else:
                    result.steps.append(f"reset --soft failed: {out[:120]}")
            else:
                result.steps.append(f"fetch failed: {out[:120]}")
        else:
            result.steps.append(f"{self.remote}/{self.branch} does not exist yet (first publish)")

        token_url = self._push_url(secrets)
        target = token_url or self.remote
        code, out = self._run(["push", target, f"HEAD:{self.branch}"], secrets=secrets)
        if code != 0:
            result.reason = f"git push failed: {out[:200]}"
            self.log.warn(f"checkpoint publish failed – {result.reason}")
            return self._finish(result, started)
        result.pushed = True
        result.ok = True
        result.reason = REASON_OK
        result.steps.append(f"pushed {self.branch}")
        self.log.info(
            f"checkpoint published to {self.remote}/{self.branch} "
            f"({result.files} file(s) staged, {result.duration_ms} ms)"
        )
        return self._finish(result, started)

    def _finish(self, result: PublishResult, started: float) -> PublishResult:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self.last_result = result
        return result


_publisher: Optional[Publisher] = None


def publisher(log: Optional[Log] = None, **kwargs: Any) -> Publisher:
    """Process-wide publisher (the checkpoint timer is per process)."""

    global _publisher
    if _publisher is None or kwargs:
        _publisher = Publisher(log=log, **kwargs)
    return _publisher


def reset_publisher() -> None:
    global _publisher
    _publisher = None


def maybe_publish(*, phase: str, done: int = 0, total: int = 0, log: Optional[Log] = None) -> PublishResult:
    """Convenience wrapper used by the checkpoint tick; never raises."""

    try:
        return publisher(log=log).maybe_publish(phase=phase, done=done, total=total)
    except Exception as exc:  # noqa: BLE001 - publishing is best-effort by design
        (log or Log("gitpush")).warn(f"checkpoint publish skipped: {type(exc).__name__}: {exc}")
        return PublishResult(reason=f"error: {exc}", attempted=True)


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """Public helper: remove secrets from *text* (used by tests and callers)."""

    return _clean(text, secrets)


__all__ = [
    "DEFAULT_BRANCH",
    "REASON_DISABLED",
    "REASON_NOTHING",
    "REASON_OK",
    "REASON_THROTTLED",
    "Publisher",
    "PublishResult",
    "enabled",
    "git_binary",
    "maybe_publish",
    "publisher",
    "reset_publisher",
    "scrub",
]
