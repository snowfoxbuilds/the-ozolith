# Spike image for TheOzolith issue #31 gate: can tailscaled run as uid-1000
# with no TUN device, no NET_ADMIN, no added capabilities, userspace
# networking, and still serve Tailscale SSH one-hop access?
#
# Deliberately: no sudo, no setcap, no sshd. If anything here turns out to
# need root at runtime, the spike has failed and that is the finding.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
 && curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
      -o /etc/apt/sources.list.d/tailscale.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends tailscale \
 && rm -rf /var/lib/apt/lists/*

# /var/lib/tailscale is chowned in the image so the named volume Docker seeds
# from it is writable by uid 1000 (gate precondition: writable --statedir).
# /spike-marker proves an incoming SSH session landed in THIS filesystem
# (gate item 2), not on the host.
RUN useradd -m -s /bin/bash -u 1000 ozolith \
 && mkdir -p /var/lib/tailscale \
 && chown 1000:1000 /var/lib/tailscale \
 && echo "spike-container-fs" > /spike-marker

COPY entrypoint.sh /usr/local/bin/spike-entrypoint
RUN chmod 0755 /usr/local/bin/spike-entrypoint

USER ozolith
ENTRYPOINT ["/usr/local/bin/spike-entrypoint"]
