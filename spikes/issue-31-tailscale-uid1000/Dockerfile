# Spike image for TheOzolith issue #31 gate: can tailscaled run as uid-1000
# with no TUN device, no NET_ADMIN, no added capabilities, userspace
# networking, and still serve Tailscale SSH one-hop access?
#
# Deliberately: no sudo, no setcap, no sshd. If anything here turns out to
# need root at runtime, the spike has failed and that is the finding.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# The EXACT archive the production example pins
# (deploy/configs-example/worker-types/flightdeck.toml, PR #35): same
# version, same URL, same fetch/verify/install sequence — gate evidence for
# one binary says nothing about another, so the tested release and the
# shipped release must be identical (a test on the #35 branch enforces the
# match). The only difference: the spike carries the REAL official checksum
# (it must actually run) where the example ships a fail-closed placeholder.
# Do not bump this version between the gate's phases (initial enrollment,
# retained-volume replacement, state-loss re-enrollment) — identity survival
# across the SAME binary is part of what the gate proves.
RUN TS_VERSION=1.102.2; TS_ARCH=amd64; \
    TS_SHA256=ad2cde12f8de95f7b93a1e0401e652291c603d42b9d60a33fb1741eb38ab04d8; \
    curl -fsSL -o /tmp/ts.tgz "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${TS_ARCH}.tgz" && \
    echo "${TS_SHA256}  /tmp/ts.tgz" | sha256sum -c - && \
    tar -xzf /tmp/ts.tgz -C /tmp && \
    install -m 0755 "/tmp/tailscale_${TS_VERSION}_${TS_ARCH}/tailscale"  /usr/local/bin/tailscale && \
    install -m 0755 "/tmp/tailscale_${TS_VERSION}_${TS_ARCH}/tailscaled" /usr/local/bin/tailscaled && \
    rm -rf /tmp/ts.tgz "/tmp/tailscale_${TS_VERSION}_${TS_ARCH}"

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
