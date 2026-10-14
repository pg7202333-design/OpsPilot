
"""Restricted sandbox: an isolated working copy of the shop that patches are applied to.

Defense in depth:
  1. Patches may only modify allowlisted service files.
  2. Edits must match exactly once.
  3. Candidate Python must compile before it is written.
  4. Subprocesses run with a scrubbed environment and resource limits.
"""
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

WRITE_ALLOWLIST = (
    "shop/catalog.py",
    "shop/config.py",
    "shop/inventory.py",
    "shop/ledger.py",
    "shop/orders.py",
    "shop/payments.py",
)

_REPLAY = '''import json, sys
from shop.workload import run_journal
journal = json.load(open(sys.argv[1]))
shop, results = run_journal(journal, int(sys.argv[2]))
json.dump({"telemetry": shop.tel.export(), "results": results}, sys.stdout)
'''


class SandboxError(Exception):
    """Raised when a sandbox operation is rejected."""


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out


class Sandbox:
    def __init__(self, edits=(), source_root=REPO_ROOT):
        self.dir = Path(tempfile.mkdtemp(prefix="opspilot-sbx-")).resolve()

        for name in ("shop", "shop_tests"):
            shutil.copytree(
                Path(source_root) / name,
                self.dir / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )

        (self.dir / "_replay.py").write_text(_REPLAY)

        for edit in edits:
            self.apply_edit(edit, trusted=True)

    # -- File access ---------------------------------------------------------

    def _resolve(self, rel):
        path = (self.dir / rel).resolve()

        if self.dir not in path.parents:
            raise SandboxError("path escapes sandbox: %s" % rel)

        return path

    def read(self, rel):
        return self._resolve(rel).read_text()

    def write_support_file(self, rel, text):
        """Write harness files that patches cannot modify."""
        path = self._resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def install_repro(self, journal, test_source):
        """Install the incident reproduction test and its journal."""
        self.write_support_file("repro/__init__.py", "")
        self.write_support_file("repro/journal.json", json.dumps(journal))
        self.write_support_file("repro/test_incident_repro.py", test_source)

    def apply_edit(self, edit, trusted=False):
        """Apply one exact-match edit, validating Python syntax first."""
        rel = edit["file"]
        path = self._resolve(rel)

        if (
            not trusted
            and path.relative_to(self.dir).as_posix() not in WRITE_ALLOWLIST
        ):
            raise SandboxError(
                "write denied (not an editable service file): %s" % rel
            )

        text = path.read_text()
        find_text = edit["find"]
        replace_text = edit["replace"]

        if text.count(find_text) != 1:
            raise SandboxError(
                "edit does not match exactly once in %s" % rel
            )

        # If a model matches only the code token (not its leading spaces), its
        # multiline replacement may omit the indentation inherited by the first
        # line. Preserve exact-match semantics, but carry that indentation onto
        # continuation lines before syntax validation.
        if "\n" not in find_text and "\n" in replace_text:
            match_at = text.find(find_text)
            line_start = text.rfind("\n", 0, match_at) + 1
            prefix = text[line_start:match_at]
            replacement_lines = replace_text.splitlines(keepends=True)
            if (prefix and prefix.strip() == ""
                    and replacement_lines
                    and replacement_lines[0].lstrip() == replacement_lines[0]):
                replace_text = replacement_lines[0] + "".join(
                    (prefix + line) if line.strip() else line
                    for line in replacement_lines[1:]
                )

        updated = text.replace(find_text, replace_text)

        # Reject malformed Python before writing it into the sandbox.
        if rel.endswith(".py"):
            try:
                compile(updated, rel, "exec")
            except SyntaxError as exc:
                raise SandboxError(
                    "patch creates invalid Python in %s: %s" % (rel, exc)
                ) from exc

        path.write_text(updated)

    def apply_patch(self, edits):
        """Apply a list of candidate edits."""
        for edit in edits:
            self.apply_edit(edit)

    # -- Execution -----------------------------------------------------------

    def run(self, argv, timeout=60, mem_mb=1024):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.dir),
        }

        def limits():
            # macOS may reject RLIMIT_AS; apply supported limits best-effort.
            wanted = [
                ("RLIMIT_CPU", timeout + 5),
                ("RLIMIT_FSIZE", 64 << 20),
            ]

            if sys.platform != "darwin":
                wanted.insert(0, ("RLIMIT_AS", mem_mb << 20))

            for name, value in wanted:
                try:
                    resource.setrlimit(
                        getattr(resource, name),
                        (value, value),
                    )
                except (ValueError, OSError, AttributeError):
                    pass

        start = time.time()

        try:
            proc = subprocess.run(
                argv,
                cwd=self.dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=limits,
            )

            return RunResult(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                False,
                time.time() - start,
            )

        except subprocess.TimeoutExpired as exc:
            return RunResult(
                -1,
                exc.stdout or "",
                exc.stderr or "",
                True,
                time.time() - start,
            )

    def replay(self, journal, seed, timeout=60):
        """Replay a journal against sandboxed code."""
        filename = "journal_%d.json" % seed
        self.write_support_file(filename, json.dumps(journal))

        result = self.run(
            [
                sys.executable,
                "_replay.py",
                filename,
                str(seed),
            ],
            timeout=timeout,
        )

        if not result.ok:
            raise SandboxError(
                "replay failed: %s"
                % (result.stderr.strip()[-400:] or "timeout")
            )

        data = json.loads(result.stdout)
        return data["telemetry"], data["results"]

    def run_tests(self, start_dir, timeout=60):
        """Run a unittest suite inside the sandbox."""
        return self.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                start_dir,
                "-t",
                ".",
                "-q",
            ],
            timeout=timeout,
        )

    # -- Lifecycle -----------------------------------------------------------

    def cleanup(self):
        """Remove the temporary sandbox directory."""
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
