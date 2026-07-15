#!/bin/sh
# Run the actor inside tmux (the worker-image convention, NODE-SUBSTRATE.md):
# any session is attachable at any time via
#   docker exec -it <container> tmux attach
# which is exactly what the M4 dashboard's PTY bridge will run.
#
# The session's output is piped to the container's stdout so `docker logs`
# keeps working. When the actor exits (recycle after N Runs, or a crash) the
# tmux session ends, this script ends, and the restart policy brings the
# container back fresh.
set -eu

tmux new-session -d -s main "$*"
tmux pipe-pane -t main -o 'cat >> /proc/1/fd/1'
while tmux has-session -t main 2>/dev/null; do
    sleep 2
done
