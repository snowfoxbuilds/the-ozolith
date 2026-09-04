"""The driver-side supervisor of one Run's GitHub Relay child (ADR-0057
items 8 and 10).

The supervisor owns everything the child must never create for itself: the
per-Run relay directory and the exclusively created audit sink, the socket
bound at the job-dir root, and the pipe the credential crosses once. The
child is spawned with those as inherited descriptors, an explicit environment
holding only what its interpreter needs to start and find the package, and
nothing secret in argv; it is watched through its report lines, and at the
end of the agent phase signalled, waited for, and classified from its exit
report and status together with what the supervisor itself observed.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from theozolith_worker.relay import audit
from theozolith_worker.relay.audit import AuditFailure, SinkExistsError
from theozolith_worker.relay.reasons import DEFAULT_BUDGETS, Budgets, Kind
from theozolith_worker.relay.server import REASON_AGENT_EXIT, REASON_CONNECTION_BUDGET
from theozolith_worker.relay.upstream import Live, NoUpstream

SOCKET_NAME = "gh.sock"
CONTAINER_SOCKET_PATH = "/job/gh.sock"
READY_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.05

# The child's whole environment: what its interpreter needs to start and
# find the package, nothing else. No driver token source, provider key,
# ``_FILE`` path, or operator variable crosses. PATH resolves a non-absolute
# interpreter (Popen looks the executable up through the environment it is
# given); PYTHONPATH and PYTHONHOME are how the parent's own interpreter
# finds the package in a source-tree run; the locale variables make the
# child decode argv — the spool path, the run id — as the parent encoded it.
INHERITED_ENVIRONMENT = ("PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL", "LC_CTYPE")

TERMINATION_CLEAN = "clean"
TERMINATION_EXHAUSTED = "exhausted"
TERMINATION_KILLED = "killed"
TERMINATION_CRASHED = "crashed"


def child_environment(parent: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The relay child's environment: the allowlisted names present in
    ``parent``, copied unchanged — never a synthesized value."""
    return {name: parent[name] for name in INHERITED_ENVIRONMENT if name in parent}


class RelayStartError(RuntimeError):
    """The relay could not be brought up for this Run: an infra-class,
    pre-work failure the caller treats as a ``SessionError`` (ADR-0016)."""


@dataclass(frozen=True)
class RelayExit:
    termination: str
    exit_status: int | None
    exit_report: dict | None
    audit_failure: AuditFailure | None


class _Reports:
    """The child's stdout, one JSON object per line, folded into the three
    facts the driver keeps: readiness, the first audit failure, the exit."""

    def __init__(self):
        self.ready = threading.Event()
        self.audit_failure: AuditFailure | None = None
        self.exit_report: dict | None = None
        self.exited = threading.Event()

    def observe(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if kind == "ready":
            self.ready.set()
        elif kind == "audit-failure" and self.audit_failure is None:
            with contextlib.suppress(KeyError, ValueError):
                self.audit_failure = AuditFailure(
                    Kind(event["kind"]), event.get("seq"), event.get("hop")
                )
        elif kind == "exit":
            self.exit_report = event
            self.exited.set()


def _pump(stream, sink: Callable[[str], None]) -> None:
    with stream:
        for raw in stream:
            sink(raw.decode("utf-8", errors="replace").rstrip("\n"))


class RelayRun:
    """One Run's relay child, from ``start`` to the ``RelayExit`` that
    ``stop`` returns. Never restarts the child, never touches the sink."""

    def __init__(
        self,
        *,
        popen: subprocess.Popen,
        socket_path: Path,
        relay_dir: Path,
        reports: _Reports,
        readers: list[threading.Thread],
    ):
        self._popen = popen
        self.socket_path = socket_path
        self.relay_dir = relay_dir
        self._reports = reports
        self._readers = readers
        self._sigterm_sent = False
        self._exit: RelayExit | None = None
        self._sink_offset = 0
        self._gh_calls = 0
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self._popen.pid

    @classmethod
    def start(
        cls,
        *,
        job: Path,
        jobs_dir: Path,
        run_id: str,
        upstream: Live | NoUpstream,
        log: Callable[[str], None],
        budgets: Budgets = DEFAULT_BUDGETS,
        python: str = sys.executable,
    ) -> RelayRun:
        try:
            relay_dir = audit.create_relay_dir(jobs_dir, run_id)
        except OSError as exc:
            raise RelayStartError(f"relay directory for {run_id}: {exc.strerror}") from exc
        spool_dir = relay_dir / audit.SPOOL_DIR
        spool_dir.mkdir(mode=0o700)
        try:
            sink_fd = audit.open_sink(relay_dir)
        except SinkExistsError as exc:
            raise RelayStartError(f"audit sink for {run_id}: {exc.strerror}") from exc

        socket_path = job / SOCKET_NAME
        if os.path.lexists(socket_path):
            # The job dir is fresh for this run_id; an entry here is a bug or
            # tampering, never something to unlink and continue (item 10).
            os.close(sink_fd)
            raise RelayStartError(f"an entry already exists at the relay socket path {SOCKET_NAME}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o666)
            listener.listen(64)
        except OSError as exc:
            listener.close()
            os.close(sink_fd)
            raise RelayStartError(f"relay socket: {exc.strerror}") from exc

        live = isinstance(upstream, Live)
        cred_r = cred_w = -1
        if live:
            cred_r, cred_w = os.pipe()
        listen_fd = listener.fileno()
        argv = [
            python,
            "-m",
            "theozolith_worker.relay",
            "--listen-fd",
            str(listen_fd),
            "--sink-fd",
            str(sink_fd),
            "--run-id",
            run_id,
            *(("--credential-fd", str(cred_r)) if live else ("--no-upstream",)),
            "--spool-dir",
            str(spool_dir),
            "--budgets",
            json.dumps(asdict(budgets)),
        ]
        try:
            popen = subprocess.Popen(
                argv,
                pass_fds=[listen_fd, sink_fd, *([cred_r] if live else [])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
                env=child_environment(),
            )
        except OSError as exc:
            listener.close()
            os.close(sink_fd)
            if live:
                os.close(cred_r)
                os.close(cred_w)
            os.unlink(socket_path)
            raise RelayStartError(f"relay child: {exc.strerror}") from exc
        listener.detach()
        os.close(listen_fd)
        os.close(sink_fd)
        if live:
            os.close(cred_r)

        reports = _Reports()
        readers = [
            threading.Thread(
                target=_pump, args=(popen.stdout, reports.observe), daemon=True, name="relay-out"
            ),
            threading.Thread(target=_pump, args=(popen.stderr, log), daemon=True, name="relay-err"),
        ]
        for reader in readers:
            reader.start()
        run = cls(
            popen=popen,
            socket_path=socket_path,
            relay_dir=relay_dir,
            reports=reports,
            readers=readers,
        )
        # From here on the child exists: whatever fails, it is killed and
        # reaped, the readers joined, the socket and spool cleared.
        try:
            if live:
                assert isinstance(upstream, Live)
                try:
                    _deliver_credential(cred_w, upstream)
                finally:
                    os.close(cred_w)
            if not run._await_ready():
                raise RelayStartError("relay child did not report ready")
        except RelayStartError:
            run._abandon()
            raise
        except (OSError, UnicodeError, ValueError):
            # ``from None`` on purpose: a FileNotFoundError carries the path
            # and a UnicodeDecodeError the bytes; neither may ride the chain.
            run._abandon()
            raise RelayStartError("relay credential could not be delivered") from None
        except BaseException:
            run._abandon()
            raise
        return run

    def _await_ready(self) -> bool:
        deadline = READY_TIMEOUT_SECONDS
        waited = 0.0
        while waited < deadline:
            if self._reports.ready.wait(_POLL_SECONDS):
                return True
            waited += _POLL_SECONDS
            if self._popen.poll() is not None:
                return self._reports.ready.is_set()
        return False

    def _abandon(self) -> None:
        """A failed start: kill the child if it still runs, reap it, join
        the readers, clean up — never classify."""
        if self._popen.poll() is None:
            _signal(self._popen.pid, signal.SIGKILL)
        self._popen.wait()
        self._join_readers()
        self._cleanup()

    # -- telemetry -------------------------------------------------------

    @property
    def gh_calls(self) -> int:
        """The running count of intent records, from an incremental scan of
        the driver-owned sink: complete lines only, a partial tail carried
        to the next read."""
        with self._lock:
            path = self.relay_dir / audit.SINK_NAME
            try:
                with path.open("rb") as handle:
                    handle.seek(self._sink_offset)
                    chunk = handle.read()
            except OSError:
                return self._gh_calls
            end = chunk.rfind(b"\n")
            if end == -1:
                return self._gh_calls
            complete = chunk[: end + 1]
            self._sink_offset += len(complete)
            parsed = audit.parse_records(complete)
            self._gh_calls += parsed.counts_by_kind.get(Kind.INTENT.value, 0)
            return self._gh_calls

    # -- exit ------------------------------------------------------------

    def poll(self) -> RelayExit | None:
        """The classified exit once the child has exited — no cleanup, that
        is ``stop``'s — else ``None``."""
        with self._lock:
            if self._exit is not None:
                return self._exit
            status = self._popen.poll()
            if status is None:
                return None
            self._join_readers()
            return self._classify(status, killed=False)

    def stop(self, *, timeout: float = 10.0) -> RelayExit:
        """End the relay: SIGTERM, a bounded wait, SIGKILL if it must be;
        then the exit classification and the cleanup of socket and spool.
        Idempotent — a second call signals nothing and returns the record."""
        with self._lock:
            if self._exit is not None:
                return self._exit
            killed = False
            status = self._popen.poll()
            if status is None:
                self._sigterm_sent = True
                _signal(self._popen.pid, signal.SIGTERM)
                try:
                    status = self._popen.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    killed = True
                    _signal(self._popen.pid, signal.SIGKILL)
                    status = self._popen.wait()
            self._join_readers()
            exit_ = self._classify(status, killed=killed)
            self._exit = exit_
            self._cleanup()
            return exit_

    def _classify(self, status: int, *, killed: bool) -> RelayExit:
        report = self._reports.exit_report
        reason = report.get("reason") if report is not None else None
        if killed:
            termination = TERMINATION_KILLED
        elif status == 0 and reason == REASON_CONNECTION_BUDGET:
            termination = TERMINATION_EXHAUSTED
        elif status == 0 and reason == REASON_AGENT_EXIT and self._sigterm_sent:
            termination = TERMINATION_CLEAN
        else:
            termination = TERMINATION_CRASHED
        return RelayExit(termination, status, report, self._reports.audit_failure)

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join()

    def _cleanup(self) -> None:
        """The socket (a backstop to the child's own unlink) and the spool
        entries; the sink is another tree's and is never touched here."""
        if os.path.lexists(self.socket_path):
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        spool_dir = self.relay_dir / audit.SPOOL_DIR
        try:
            entries = list(spool_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            with contextlib.suppress(OSError):
                entry.unlink()


def _deliver_credential(cred_w: int, upstream: Live) -> None:
    """The credential crosses the pipe once: never argv, never the child's
    environment, never a file the supervisor writes. The caller closes the
    write end behind it, on every path, so the child reads end-of-file."""
    if upstream.credential is not None:
        credential = upstream.credential
    else:
        assert upstream.credential_file is not None
        credential = upstream.credential_file.read_text(encoding="utf-8").strip()
    data = credential.encode("utf-8")
    while data:
        written = os.write(cred_w, data)
        data = data[written:]


def _signal(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, sig)
