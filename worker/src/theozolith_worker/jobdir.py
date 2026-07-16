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
        manifest.json    what to run: mode, session, adapter, model, timeouts
        prompt.md        the prompt the harness pastes into the session
        jobs/            driver-sequenced job requests (gate steps, shutdown)
      output/
        status.json      harness phase + agent outcome (atomic writes)
        transcript.txt   tmux pipe-pane capture of the whole session
        hook-events.log  completion-hook events (adapter-specific)
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

# Harness phases, in order of appearance in output/status.json.
PHASE_STARTING = "starting"
PHASE_AGENT = "agent"
PHASE_SERVING_JOBS = "serving-jobs"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# The job request that tells the harness to stop serving and exit.
SHUTDOWN_JOB = "shutdown"

MANIFEST_FILE = "input/manifest.json"
PROMPT_FILE = "input/prompt.md"
STATUS_FILE = "output/status.json"
TRANSCRIPT_FILE = "output/transcript.txt"
HOOK_EVENTS_FILE = "output/hook-events.log"
VERDICT_FILE = "output/verdict.json"
JOBS_IN_DIR = "input/jobs"
JOBS_OUT_DIR = "output/jobs"
CHECKOUT_DIR = "checkout"
WORK_DIR = "work"

DEFAULT_AGENT_TIMEOUT = 3600.0
DEFAULT_SETTLE_SECONDS = 20.0
DEFAULT_STARTUP_SECONDS = 5.0
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
    session: str  # tmux session name (run-<run-id> | review-<pr>-round-<n>)
    adapter: str  # harness adapter name (M2: "claude")
    model: str
    workdir: str = CHECKOUT_DIR  # job-dir-relative agent working directory
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT
    settle_seconds: float = DEFAULT_SETTLE_SECONDS
    startup_seconds: float = DEFAULT_STARTUP_SECONDS
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
    """How the agent session ended, as the harness observed it."""

    completed: bool = False  # the completion hook fired and settled
    timed_out: bool = False  # the hard timeout cut the session short
    session_died: bool = False  # the agent process exited on its own

    def describe(self) -> str:
        if self.completed:
            return "completed"
        if self.timed_out:
            return "timed out"
        if self.session_died:
            return "session died"
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
        agent = AgentOutcome(
            completed=bool(raw_agent.get("completed")),
            timed_out=bool(raw_agent.get("timed_out")),
            session_died=bool(raw_agent.get("session_died")),
        )
    return Status(phase=data["phase"], agent=agent, error=str(data.get("error", "")))


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
