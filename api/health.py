import os
import time
import urllib.parse
from fastapi import APIRouter, Request

router = APIRouter(tags=["Health & Keepalive"])

_STARTED_AT = time.monotonic()


def _resolve_target_url(request: Request) -> str:
    env_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if env_url:
        return f"{env_url.rstrip('/')}/health"
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    if host:
        return f"https://{host.rstrip('/')}/health"
    return "https://quizbot-o1mt.onrender.com/health"


@router.get("/health")
@router.get("/api/health")
def health_check(request: Request):
    uptime_sec = round(time.monotonic() - _STARTED_AT, 2)
    return {
        "status": "ok",
        "service": "quizbot",
        "uptime_sec": uptime_sec,
        "keepalive_url": _resolve_target_url(request),
    }


@router.get("/health/cron-config")
@router.get("/api/health/cron-config")
def cron_config(request: Request):
    target_url = _resolve_target_url(request)
    parsed = urllib.parse.urlparse(target_url)
    return {
        "method": "GET",
        "url": target_url,
        "host": parsed.netloc,
        "recommended_interval_minutes": 10,
        "note": "Use an external scheduler (such as cron-job.org or UptimeRobot) on this URL; Render free tier still has a monthly runtime quota.",
    }
