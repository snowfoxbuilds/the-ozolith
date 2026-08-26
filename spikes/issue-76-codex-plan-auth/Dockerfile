# Spike image for issue #76: prove ChatGPT-plan auth works for headless
# `codex exec` under the run-container posture (uid-1000 ozolith user, slim
# base) and capture the `--json` stream fixtures the CodexAdapter parsers
# will be written against. This image mirrors worker/docker/Dockerfile.claude's
# shape deliberately — auth and refresh behavior under a non-root user is part
# of what the spike proves — but installs no theozolith packages: the spike
# exercises the codex CLI alone.
#
# The CLI version is PINNED: the `--json` event schema is the contract the
# adapter parsers speak, and the spike's evidence is only valid for the
# version it ran against. Bump deliberately, re-running the spike.
#
# Build from this directory:
#   docker build -t ozolith-codex-spike:latest .
#
# No credential enters the build context or the image. auth.json reaches
# containers only at run time — via the CODEX_AUTH_JSON environment variable
# (design a, exactly what CodexAdapter.prepare() will do) or the spike's
# auth volume (S1 login state).

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@0.150.0

RUN useradd --create-home --shell /bin/bash --uid 1000 ozolith

COPY entrypoint.sh /usr/local/bin/spike-entrypoint
RUN chmod 0755 /usr/local/bin/spike-entrypoint

USER ozolith
ENV HOME=/home/ozolith
WORKDIR /home/ozolith

ENTRYPOINT ["spike-entrypoint"]
