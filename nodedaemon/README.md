# theozolith-nodedaemon

Node Daemon (renamed from "Node Agent" — Agent is reserved for LLM tool configs;
ADR-0013): the uncontainerized TheOzolith daemon installed on every physical node,
registering it as a Container-Host — heartbeats, infrastructure-command reconciliation,
local derived-image builds, stack + driver supervision (cgroup kill-the-tree), and
labeled run-container reaping. See `docs/specs/NODE-SUBSTRATE.md`.

**Stub in M2** — the component lands in M3.
