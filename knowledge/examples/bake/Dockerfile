# Sample: bake a pinned Knowledge Source into an image at build time.
#
# Build from the repo root:
#   docker build -f knowledge/examples/bake/Dockerfile \
#     --build-arg KNOWLEDGE_SOURCE=https://example.com/my-knowledge.git \
#     --build-arg KNOWLEDGE_PIN=<commit-sha> \
#     .
#
# The bake runs entirely at build time; nothing syncs or sets up at container
# start (NODE-SUBSTRATE.md, extension points).

FROM python:3.13-slim

# git is required by the bake CLI at build time only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install the knowledge machinery from the build context.
COPY knowledge /opt/theozolith-knowledge
RUN pip install --no-cache-dir /opt/theozolith-knowledge

# Bake the pinned Knowledge Source into the image's Claude config dir.
ARG KNOWLEDGE_SOURCE
ARG KNOWLEDGE_PIN
RUN theozolith-knowledge bake \
    --source "$KNOWLEDGE_SOURCE" \
    --pin "$KNOWLEDGE_PIN" \
    --target /root/.claude
