"""Entry point: python -m monitor"""

import logging
import signal
import sys

from .config import Config
from .monitor import Monitor
from .server import serve


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("monitor")
    try:
        cfg = Config.from_env()
    except ValueError as e:
        log.error("configuration error: %s", e)
        sys.exit(2)
    if not cfg.auth_password:
        log.warning("MONITOR_PASSWORD is not set: the dashboard is open to "
                    "anyone who can reach the port")

    monitor = Monitor(cfg)
    monitor.start()
    httpd = serve(monitor, cfg)

    # As PID 1 in a container SIGTERM is ignored unless handled.
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        monitor.save()
        log.info("stopped")


if __name__ == "__main__":
    main()
