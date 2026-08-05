"""The Operator TUI (M9): ``theozolith top``, the routine-operations surface.

A full-screen Textual application consuming the control-plane read models
over loopback with the admin bearer token — a pure API consumer with the
same auth and endpoints as any remote client (NODE-SUBSTRATE.md § Operator
TUI, ADR-0040). The discipline is test-enforced across this package:

- every datum arrives via ``GET /api/v1/state`` and ``GET /api/v1/events``;
  no module here imports the store, the secret store, or the web surface,
  and neither control-plane database file is ever opened or even named;
- the three writes are the existing endpoints (commands, quarantine
  release, secret entry) — nothing else mutates anything;
- no embedded terminal: no ``pty``, no websocket, no subprocess — attach
  assistance PRINTS a pastable command resolved from heartbeat state.

URL, token, and CA resolve through the one implementation ``status`` uses
(``statuscli.resolve_target`` via ``cli._admin_env``), so ``sudo theozolith
top`` works with no environment on the Control Node and unmodified against
an SSH-forwarded socket (``CONTROL_NODE_URL`` + ``THEOZOLITH_ADMIN_TOKEN``
+ ``THEOZOLITH_TLS_CA``).

Textual is imported only under ``tui.app`` — the data layer (``client``,
``model``) stays importable everywhere control/ is, and ``worker/``,
``nodedaemon/``, and ``knowledge/`` never see any of it (ADR-0015 as
amended: the dependency exception is control-only).
"""
