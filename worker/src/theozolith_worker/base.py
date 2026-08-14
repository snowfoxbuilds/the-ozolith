"""The Worker base class: the node-resident dispatch-run loop (ADR-0020).

``Worker`` is the base abstraction for every automated actor in the pipeline
(ADR-0020): a credentialed, node-resident driver that requests work from the
Control Node (ADR-0017), runs it in ephemeral containers (ADR-0013), and owns
the GitHub writes its transition set demands. Built-in worker types express
their variance by subclassing — the loop, the boot evidence sweep, the
unreachable-Control backoff, and the pass-level error surfacing are shared
here; the three seams a type overrides are the work query (``fetch_work``),
the per-item transition set (``execute``), and the idle-pass hook
(``on_idle``). The base writes no GitHub labels: the transition set is the
subclass's alone.

Custom worker types extend this class through ``theozolith_worker.api``
(ADR-0042); it is the stable code-level extension surface.
"""

from __future__ import annotations

import secrets
import shutil
import time
import traceback
from pathlib import Path
from typing import ClassVar

from theozolith_worker import jobdir
from theozolith_worker.config import DriverConfig, load_config
from theozolith_worker.containers import ContainerSpec, DockerEngine, container_labels
from theozolith_worker.dispatch import DispatchClient, WorkDispatch, backoff_delay
from theozolith_worker.events import EventSink, emit_error, make_sink
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.identity import identity_error_detail
from theozolith_worker.sessions import SessionError, SessionFactory, container_session_factory
from theozolith_worker.sweep import sweep_orphans

# The setup dry-run's own session budget: identity checks plus one probe
# turn — minutes at most, never an agent-length wait.
DRYRUN_TIMEOUT_SECONDS = 600.0


def _log(message: str) -> None:
    print(message, flush=True)


class Worker:
    """One driver's poll-claim-run loop; subclasses fill the ADR-0020 seams."""

    role: ClassVar[str]  # env prefix + dispatch role ("implementer", "reviewer")
    # No default_model: the model is a worker-type-definition field baked into
    # the run image at build time (ADR-0045); driver code never selects one.

    # The Implementer drains its queue one claim per pass (no sleep between
    # grants); the base default sleeps every pass. A type that fetches its
    # whole work-list at once (the Reviewer) keeps the default.
    sleep_after_work: ClassVar[bool] = True
    # The pass-level error summary's noun ("dispatch pass failed" / "review
    # pass failed"): the message a swallowed pass exception surfaces.
    pass_label: ClassVar[str] = "dispatch"

    def __init__(
        self,
        config: DriverConfig,
        *,
        client: GitHubClient | None = None,
        session_factory: SessionFactory | None = None,
        dispatch: WorkDispatch | None = None,
        sink: EventSink | None = None,
        log=_log,
    ):
        self.config = config
        self.log = log
        self.client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
        self.session_factory = session_factory or container_session_factory(DockerEngine())
        self.sink = sink or make_sink(config, log)
        self.dispatch = dispatch or DispatchClient(
            config.control_node_url,
            config.control_token,
            ca=config.control_ca,
            log=log,
            on_error=lambda error_class, message: emit_error(
                self.sink, config, error_class=error_class, message=message
            ),
        )
        self.me = ""  # this driver's GitHub login; resolved in run()

    @classmethod
    def load(cls, environ=None) -> Worker:
        """Build this worker type from the environment (VAR_FILE honored)."""
        return cls(load_config(environ, role=cls.role))

    def run(self, *, once: bool = False, sleep=time.sleep) -> int:
        """The dispatch-run loop; returns the number of items executed.

        Boot evidence sweep, then the identity dry-run (once per driver
        process — while it fails, no work is fetched and it is retried each
        pass), then each pass: fetch work (None = Control unreachable),
        execute each item, surface any escaped exception as a
        theozolith.error, then idle-hook + unreachable-backoff. ``once`` runs a
        single pass. A reachable Control Node is required either way (ADR-0017):
        with it down, in-flight work finishes and new work pauses.
        """
        self.me = self.client.viewer_login()
        self._startup_log()
        self.on_boot()

        count = 0
        unreachable_streak = 0
        identity_verified = False
        while True:
            if not identity_verified:
                identity_verified = self._verify_image_identity()
                if not identity_verified:
                    if once:
                        return count
                    sleep(backoff_delay(self.config.poll_seconds, 0))
                    continue
            items: list | None = None
            try:
                items = self.fetch_work()
                for item in items or []:
                    count += self.execute(item)
            except Exception as exc:
                # The pass-level summary: GitHub write failures in escalation
                # paths and anything else that escaped an item's own failure
                # handling surfaces here (2026-07-21 grilling).
                self.log(f"{self.pass_label} pass failed: {exc}")
                emit_error(
                    self.sink,
                    self.config,
                    error_class=type(exc).__name__,
                    message=f"{self.pass_label} pass failed: {exc}",
                    context=traceback.format_exc(),
                )
            if once:
                return count
            unreachable_streak = unreachable_streak + 1 if items is None else 0
            if items:
                # Work happened: the Implementer re-polls immediately to drain
                # its queue; other types sleep out the poll interval.
                if self.sleep_after_work:
                    sleep(backoff_delay(self.config.poll_seconds, unreachable_streak))
            else:
                self.on_idle()
                sleep(backoff_delay(self.config.poll_seconds, unreachable_streak))

    def _verify_image_identity(self) -> bool:
        """The setup dry-run (ADR-0045, best effort): one identity-dryrun
        container per driver process, BEFORE any work is taken — the
        identity checks, the CLI floor, and the one-time probe session run
        with the real image and credential, so a broken combination fails
        loud here in seconds instead of burning claims and Runs. While it
        fails, the driver fetches no work and retries each pass. The
        dot-prefixed job dir is invisible to the evidence sweep and to
        queue-behind; model-less images pass trivially."""
        jobs_root = Path(self.config.jobs_dir)
        jobs_root.mkdir(parents=True, exist_ok=True)
        name = f".identity-dryrun-{secrets.token_hex(4)}"
        job = jobdir.create_job_dir(jobs_root, name)
        try:
            manifest = jobdir.Manifest(
                run_id=name.lstrip("."),
                mode=jobdir.MODE_DRYRUN,
                adapter=self.config.adapter,
                agent_timeout_seconds=DRYRUN_TIMEOUT_SECONDS,
            )
            jobdir.write_manifest(job, manifest)
            spec = ContainerSpec(
                name=f"ozolith-identity-{secrets.token_hex(4)}",
                image=self.config.run_image,
                labels=container_labels(manifest.run_id, self.config.stack),
                mounts=((str(job), jobdir.CONTAINER_JOB_PATH),),
                volumes=self.config.cache_volumes,
                env=dict(self.config.agent_env),  # never the GitHub PAT (ADR-0013)
                user=self.config.container_user,
            )
            session = self.session_factory(spec, job, manifest)
            session.launch()
            try:
                session.wait_for_agent()
            finally:
                session.finish()
        except SessionError as exc:
            detail = identity_error_detail(str(exc)) or str(exc)
            self.log(
                f"identity dry-run FAILED for {self.config.run_image}: {detail}"
                " — no work will be fetched until it passes"
            )
            emit_error(
                self.sink,
                self.config,
                error_class="IdentityDryRun",
                message=f"identity dry-run failed for {self.config.run_image}: {detail}",
            )
            return False
        except Exception as exc:  # container-engine or filesystem breakage
            self.log(
                f"identity dry-run could not execute ({exc})"
                " — no work will be fetched until it passes"
            )
            emit_error(
                self.sink,
                self.config,
                error_class=type(exc).__name__,
                message=f"identity dry-run could not execute: {exc}",
            )
            return False
        else:
            record = jobdir.read_identity(job) or {}
            baked = record.get("expected_model") or "(no baked identity)"
            self.log(f"identity dry-run passed for {self.config.run_image} ({baked})")
            return True
        finally:
            shutil.rmtree(job, ignore_errors=True)

    # -- ADR-0020 seams ---------------------------------------------------------

    def fetch_work(self) -> list | None:
        """This pass's work-list; None means the Control Node was unreachable
        (the backoff signal), [] means reachable but nothing eligible."""
        raise NotImplementedError

    def execute(self, item) -> int:
        """Apply this type's transition set to one work item; return the
        number of Runs/verdicts it produced."""
        raise NotImplementedError

    def on_idle(self) -> None:
        """Run once per idle pass (nothing fetched). Default: nothing."""

    # -- lifecycle hooks --------------------------------------------------------

    def on_boot(self) -> None:
        """Boot-time evidence sweep (ADR-0016): recover orphaned job dirs.
        Overridden by types that need the swept-and-kept result."""
        sweep_orphans(self.config, log=self.log)

    def _startup_log(self) -> None:
        self.log(f"{self.role} driver {self.config.worker_id} ({self.me}) for {self.config.repo}")
