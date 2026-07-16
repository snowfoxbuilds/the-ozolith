"""Node Daemon: the uncontainerized TheOzolith daemon on every physical node.

Registers the box as a Container-Host; heartbeats node, Stack, and labeled
run-container status; reconciles infrastructure commands; supervises
declarative Stacks (container workloads and the driver processes, in its
cgroup); builds derived images locally; materializes node-scoped secrets in
tmpfs; reaps orphaned run containers. See NODE-SUBSTRATE.md and ADR-0013/0015.
"""

__version__ = "0.3.0"
