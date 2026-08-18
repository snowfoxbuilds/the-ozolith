"""The pinned-build write lock (ADR-0048 amendment): one exclusive lock every
supported writer of the machine-owned pinned build (``configs/``) holds for
its whole read-modify-commit transaction.

The writers: ``theozolith config ingest`` (the harvest -> commit -> publish
transaction), the machine ``[control]`` address writers (init / origin-init /
``recover --ip`` via ``controltoml``), and the product-pin writer
(``theozolith update`` / ``theozolith build`` / ``ensure_pin`` via
``product``). Serialized under one lock, no supported writer can ever commit
between another writer's read of HEAD and its own commit — a concurrent
attempt fails cleanly instead of overwriting or orphaning the other writer's
commit. (Ingest additionally publishes its ref move compare-and-swap against
the HEAD it started from, so even an UNSUPPORTED writer — a hand-run ``git
commit`` racing the transaction — is never orphaned.)

The lock is a ``flock`` on the repository DIRECTORY's fd, not on a lock
file: the writers run as different users (a sudo-run CLI ingest vs the
service user's server process writing a product pin on
``POST /api/v1/product/update``), and a root-created lock file would be
unopenable for write by the service user forever after. Every writer can
open the directory it writes ``O_RDONLY``, ``flock`` accepts directory fds,
and no lock artifact is ever left in or beside the machine-owned tree.

Acquisition is NON-BLOCKING by doctrine: a contending writer is refused
loudly with a retry message rather than queued — an ingest legitimately
holds the lock across registry round-trips, and a server request must never
sit blocked behind it.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class RepoLockError(RuntimeError):
    """The pinned-build write lock is held by another writer (or the
    repository directory cannot be opened to lock it)."""


@contextlib.contextmanager
def pinned_write_lock(repo_dir: Path, *, writer: str):
    """Hold the exclusive pinned-build write lock on ``repo_dir`` for the
    caller's whole transaction. ``writer`` names the refused attempt in the
    contention error. The directory is created if missing — every writer
    creates it before writing anyway, and locking must not race that."""
    repo_dir = Path(repo_dir)
    try:
        repo_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(repo_dir), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RepoLockError(f"cannot open {repo_dir} to take the write lock: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RepoLockError(
                f"another pinned-build writer is already running for {repo_dir}"
                f" (refused: {writer}) — writers are serialized: a `theozolith"
                " config ingest`, a product-pin update, or a control-address"
                " write holds the lock; retry when it finishes"
            ) from exc
        yield
    finally:
        os.close(fd)
