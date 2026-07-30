# The Control Node image: the `control` built-in Stack (a container Stack —
# the one TheOzolith service that is always containerized). Build from the
# repo root:
#
#   docker build -f control/docker/Dockerfile -t theozolith-control:local .
#
# State lives under /data — mount the host's ~/.theozolith there (ADR-0024:
# configs/ secrets/ cache/ logs/, incl. the Config Repo at /data/configs).
# First run: `docker compose run --rm control init` (ADR-0023) generates
# master key, origin, CA/TLS, and the admin password, then prints the
# operator handoff; `serve` refuses to start uninitialized.

FROM python:3.13-slim

# git: reading the Config Repo's HEAD commit for desired-state versioning.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY knowledge /opt/theozolith/knowledge
COPY worker /opt/theozolith/worker
COPY control /opt/theozolith/control
RUN pip install --no-cache-dir \
    /opt/theozolith/knowledge /opt/theozolith/worker /opt/theozolith/control

RUN useradd --create-home --uid 1000 ozolith \
    && mkdir -p /data && chown ozolith:ozolith /data
USER ozolith

ENV THEOZOLITH_DATA_DIR=/data

EXPOSE 8443 6965
ENTRYPOINT ["theozolith"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8443"]
