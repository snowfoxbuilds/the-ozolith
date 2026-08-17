"""The per-Run job directory: the only channel between driver and harness.

ADR-0013: the trusted driver and the credential-free agent harness share no
network channel and no process tree — just this directory, bind-mounted into
the run container at ``/job``. The driver materializes ``input/`` before the
container starts; the harness writes ``output/`` as the Run progresses; gate
steps travel as job files under ``input/jobs/`` answered under
``output/jobs/``. All schemas here are the wire format both sides speak
(formats settled in ADR-0014).

Layout::

    jobs/<run-id>/
      input/
        manifest.json    what to run: mode, adapter, timeouts (the model is
                         baked into the run image, never here — ADR-0045)
        prompt.md        the driver-rendered task; the invocation argv
                         carries only a constant-size pointer to this file
        issue.json       machine-readable issue metadata (the boot-time
                         evidence sweep files parked dirs by it, ADR-0016)
        issue/           the Context Tree (#52): the issue snapshot — body,
                         authorized (OWNER/MEMBER) comments, timeline —
                         re-read at checkout
        pr/              Context Tree, resume rounds only: PR body, plus
                         authorized conversation/review comments/reviews,
                         commits, checks + statuses
        jobs/            driver-sequenced job requests (gate steps, shutdown)
      output/
        status.json      harness phase + agent outcome (atomic writes)
        transcript.txt   the headless session's structured output stream
        verdict.json     review mode: the agent's verdict, copied out
        jobs/            job results, one file per answered request
      checkout/          run mode: the token-free repo checkout (driver-made)
      work/              review mode: the session workspace (driver-seeded)

Writes that the other side polls for (status, job files) are atomic:
tmp file + rename, so a reader never sees a partial JSON document.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Where the job directory is mounted inside every run container.
CONTAINER_JOB_PATH = "/job"

MODE_RUN = "run"
MODE_REVIEW = "review"
# The setup dry-run (ADR-0045): the harness runs the identity checks and
# the one-time probe session, writes identity.json + status, and exits —
# no task file, no workdir, no agent process. Driven once per driver
# process per run image, from a dot-prefixed job dir the evidence sweep
# and queue-behind ignore.
MODE_DRYRUN = "identity-dryrun"

# Harness phases, in order of appearance in output/status.json.
PHASE_STARTING = "starting"
PHASE_AGENT = "agent"
PHASE_SERVING_JOBS = "serving-jobs"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

MANIFEST_FILE = "input/manifest.json"
PROMPT_FILE = "input/prompt.md"
STATUS_FILE = "output/status.json"
TRANSCRIPT_FILE = "output/transcript.txt"
VERDICT_FILE = "output/verdict.json"
# The baked-identity verdict (ADR-0045): expected vs effective model/effort,
# preflight status, and the gate outcome — written by the harness, embedded
# into the driver's evidence bundle. Absent when the image bakes no identity.
IDENTITY_FILE = "output/identity.json"
JOBS_IN_DIR = "input/jobs"
JOBS_OUT_DIR = "output/jobs"
CHECKOUT_DIR = "checkout"
WORK_DIR = "work"

DEFAULT_AGENT_TIMEOUT = 3600.0
DEFAULT_JOBS_IDLE_TIMEOUT = 600.0


class JobDirError(RuntimeError):
    """A job-directory artifact is missing or unreadable."""


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` so a concurrent reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Manifest:
    """input/manifest.json: everything the harness needs to run the session."""

    run_id: str
    mode: str  # "run" | "review"
    adapter: str  # Agent adapter name (M2: "claude")
    # No model field (ADR-0045): the model is baked into the run image's
    # native adapter config at build time; nothing at invocation selects one.
    workdir: str = CHECKOUT_DIR  # job-dir-relative agent working directory
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT
    jobs_idle_timeout_seconds: float = DEFAULT_JOBS_IDLE_TIMEOUT
    # Review mode: the round this session judges, so the in-session
    # validate-verdict job applies the final-round rule exactly as the
    # driver will (ADR-0014). 0/0 = not a review round.
    round: int = 0
    round_budget: int = 0

    @property
    def serve_jobs(self) -> bool:
        return self.mode == MODE_RUN

    @property
    def final_round(self) -> bool:
        return self.round_budget > 0 and self.round >= self.round_budget


def write_manifest(job: Path, manifest: Manifest) -> None:
    atomic_write(job / MANIFEST_FILE, json.dumps(asdict(manifest), indent=2, sort_keys=True))


def read_manifest(job: Path) -> Manifest:
    data = _read_json(job / MANIFEST_FILE)
    if data is None:
        raise JobDirError(f"missing or unreadable {MANIFEST_FILE} in {job}")
    try:
        return Manifest(**data)
    except TypeError as exc:
        raise JobDirError(f"bad manifest in {job}: {exc}") from exc


@dataclass(frozen=True)
class AgentOutcome:
    """How the headless agent process ended, as the harness observed it.

    Completion is process exit (ADR-0019): exit 0 is a completed session,
    a non-zero exit is a died session, and the hard timeout kills an
    overrunning process.
    """

    completed: bool = False  # the agent process exited 0
    timed_out: bool = False  # the hard timeout killed the process
    session_died: bool = False  # the agent process exited non-zero
    exit_code: int | None = None

    def describe(self) -> str:
        if self.completed:
            return "completed"
        if self.timed_out:
            return "timed out"
        if self.session_died:
            code = f" (exit {self.exit_code})" if self.exit_code is not None else ""
            return f"session died{code}"
        return "unknown"


@dataclass(frozen=True)
class Status:
    """output/status.json: the harness's phase, updated atomically."""

    phase: str
    agent: AgentOutcome | None = None
    error: str = ""


def write_status(job: Path, status: Status) -> None:
    data: dict = {"phase": status.phase, "error": status.error}
    if status.agent is not None:
        data["agent"] = asdict(status.agent)
    atomic_write(job / STATUS_FILE, json.dumps(data, indent=2, sort_keys=True))


def read_status(job: Path) -> Status | None:
    data = _read_json(job / STATUS_FILE)
    if data is None or not isinstance(data.get("phase"), str):
        return None
    agent = None
    raw_agent = data.get("agent")
    if isinstance(raw_agent, dict):
        raw_code = raw_agent.get("exit_code")
        agent = AgentOutcome(
            completed=bool(raw_agent.get("completed")),
            timed_out=bool(raw_agent.get("timed_out")),
            session_died=bool(raw_agent.get("session_died")),
            exit_code=int(raw_code) if isinstance(raw_code, int) else None,
        )
    return Status(phase=data["phase"], agent=agent, error=str(data.get("error", "")))


def write_identity(job: Path, data: dict) -> None:
    """output/identity.json: the baked-identity verdict (ADR-0045)."""
    atomic_write(job / IDENTITY_FILE, json.dumps(data, indent=2, sort_keys=True))


def read_identity(job: Path) -> dict | None:
    return _read_json(job / IDENTITY_FILE)


@dataclass(frozen=True)
class JobRequest:
    """One driver-sequenced command for the harness to run in the workdir."""

    name: str  # unique within the Run; doubles as the result file name
    command: str = ""  # shell command; empty for the shutdown request
    timeout_seconds: float = 900.0


@dataclass(frozen=True)
class JobResult:
    name: str
    ok: bool
    exit_code: int
    output: str = ""


def write_job_request(job: Path, request: JobRequest) -> None:
    atomic_write(
        job / JOBS_IN_DIR / f"{request.name}.json",
        json.dumps(asdict(request), indent=2, sort_keys=True),
    )


def read_job_request(path: Path) -> JobRequest | None:
    data = _read_json(path)
    if data is None or not isinstance(data.get("name"), str):
        return None
    return JobRequest(
        name=data["name"],
        command=str(data.get("command", "")),
        timeout_seconds=float(data.get("timeout_seconds", 900.0)),
    )


def write_job_result(job: Path, result: JobResult) -> None:
    atomic_write(
        job / JOBS_OUT_DIR / f"{result.name}.json",
        json.dumps(asdict(result), indent=2, sort_keys=True),
    )


def read_job_result(job: Path, name: str) -> JobResult | None:
    data = _read_json(job / JOBS_OUT_DIR / f"{name}.json")
    if data is None or not isinstance(data.get("name"), str):
        return None
    return JobResult(
        name=data["name"],
        ok=bool(data.get("ok")),
        exit_code=int(data.get("exit_code", -1)),
        output=str(data.get("output", "")),
    )


def pending_job_requests(job: Path) -> list[Path]:
    """Request files with no matching result yet, in driver-sequenced order."""
    in_dir = job / JOBS_IN_DIR
    out_dir = job / JOBS_OUT_DIR
    if not in_dir.is_dir():
        return []
    answered = {p.name for p in out_dir.glob("*.json")} if out_dir.is_dir() else set()
    return sorted(
        (p for p in in_dir.glob("*.json") if p.name not in answered and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def create_job_dir(root: Path, run_id: str) -> Path:
    """Materialize the empty layout for one Run under ``root``."""
    job = root / run_id
    for sub in (JOBS_IN_DIR, JOBS_OUT_DIR):
        (job / sub).mkdir(parents=True, exist_ok=True)
    return job
