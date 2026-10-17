"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import documents, system
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import init_db
from app.services import jobs
from app.services.ocr_service import ocr_service
from app.services.structure_service import structure_analyzer

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    version = init_db()
    jobs.recover_interrupted()
    jobs.purge_expired()
    retention = None
    if settings.retention_days > 0:
        retention = jobs.RetentionThread()
        retention.start()
    for problem in settings.security_problems():
        logger.error("configuration problem: %s", problem["code"])
    engine_problems = settings.engine_problems(settings.extraction_engine)
    for problem in engine_problems:
        # Uploads with this engine are refused with the same code; nothing falls back silently.
        logger.warning("default engine '%s' unavailable: %s", settings.extraction_engine, problem["code"])
    if settings.auth_mode == "none":
        logger.warning("AUTH_MODE=none: every caller is the single local user; use only on a trusted machine")
    logger.info(
        "ready: schema=v%s engine=%s ocr=%s structure=%s",
        version, settings.extraction_engine, ocr_service.backend_name, structure_analyzer.model,
    )
    yield
    if retention is not None:
        retention.stop_event.set()
    jobs.runner.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version=system.VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Disposition"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.url.path.startswith(settings.api_prefix):
        # Statement data must not linger in shared caches.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(documents.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"code": {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error"),
                  "message": str(detail), "params": {}}
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Name the offending fields; never echo the submitted values back.
    fields = sorted({".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")) for err in exc.errors()})
    return JSONResponse(status_code=422, content={"detail": {
        "code": "invalid_request", "message": "The request is not valid.", "params": {"fields": fields}}})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals or statement content to the client.
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "server_error", "message": "Something went wrong. Please try again.", "params": {}}},
    )
