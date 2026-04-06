"""Health check step handler."""

import logging

from ..executor import step_handler
from ..health import check_http, check_service_active

logger = logging.getLogger("zenplus.updater")


@step_handler("health_check")
def health_check_step(step: dict, extract_dir: str, cfg) -> None:
    url = step.get("url")
    service = step.get("service")
    timeout = step.get("timeout", 30)

    if url:
        check_http(url, timeout=timeout)
    elif service:
        check_service_active(service)
    else:
        raise ValueError("health_check requires 'url' or 'service'")
