FROM python:3.12-slim

LABEL org.opencontainers.image.title="server-monitor" \
      org.opencontainers.image.description="Lightweight hardware monitor with a web dashboard" \
      org.opencontainers.image.source="https://github.com/mediadnan/server-monitor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8088

WORKDIR /app
COPY monitor ./monitor

# Runs as root on purpose: the RAPL energy counters used for the measured CPU
# power are readable by root only. The compose file drops every capability
# and makes the filesystem read-only, so root here can read files but little
# else. Set `user: "65534:65534"` if you prefer and accept estimated CPU power.

EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8088'), timeout=3)"]

CMD ["python", "-m", "monitor"]
