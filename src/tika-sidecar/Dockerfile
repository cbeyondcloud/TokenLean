# Standard Tika image — no OCR bundle needed (only text-based docs flow through this pipeline).
# The -full tag (Tesseract OCR) added ~1 GB with no benefit for this workload.
# 3.2.2 or later: CVE-2025-54988 / CVE-2025-66516 (XXE in PDF parsing) cover 1.13 to 3.2.1, and
# this server parses documents tenants upload. Pinned by digest; Dependabot proposes the tag's
# new digest for review.
FROM apache/tika:3.3.3.0@sha256:326b238b37904c94ff61fed87afe0af72e6a29fa715933b10af97b7d56f1d4cb

EXPOSE 9998

# Tool-independent healthcheck (bash /dev/tcp) — wget absent in standard image.
# Same pattern used by qdrant in docker-compose.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD bash -c 'exec 3<>/dev/tcp/localhost/9998' || exit 1

LABEL maintainer="token-optimisation-proxy" \
      component="tika-sidecar" \
      version="3.3.1"

ENV TIKA_MAX_FILE_SIZE=100000000 \
    TIKA_TIMEOUT_MS=30000

# The base image's own unprivileged user, stated here so a change of base cannot quietly make
# this server, which parses tenants' uploads, run as root.
USER 35002:35002
