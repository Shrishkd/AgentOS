import logging
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as PoolTimeout
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from .config import Settings
from .health import Readiness, check_readiness, dependency_checks
from .logging import logging_config
from .domain import DomainError
from .registry import router as registry
from .runs import router as runs
from .worker_api import router as workers
from .model_api import router as model_execution

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_async_engine(settings.database_url(), pool_pre_ping=True)
        redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2)
        try:
            app.state.probes = dependency_checks(engine, redis)
            app.state.engine = engine
            app.state.redis = redis
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="AgentOS control plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(registry)
    app.include_router(runs)
    app.include_router(workers)
    app.include_router(model_execution)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                       "request_id": request.state.request_id}}, exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        fields = ["/".join(str(part) for part in err["loc"]) for err in exc.errors()[:8]]
        return await domain_error(request, DomainError(422, "invalid_request", "Invalid input in " + ", ".join(fields)))

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        try:
            request_id = str(UUID(request.headers.get("x-request-id", "")))
        except ValueError:
            request_id = str(uuid4())
        start = perf_counter()
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except (OperationalError, InterfaceError, PoolTimeout, ConnectionError, TimeoutError) as exc:
            logger.error("database_request_failed", extra={"request_id": request_id, "error_type": type(exc).__name__})
            response = await domain_error(request, DomainError(503, "storage_unavailable", "Storage is unavailable."))
        except Exception as exc:
            logger.error("request_failed", extra={"request_id": request_id, "error_type": type(exc).__name__})
            response = JSONResponse(
                {"error": {"code": "internal_error", "message": "Request failed.", "request_id": request_id}},
                status_code=500,
            )
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        logger.info("request", extra={
            "request_id": request_id, "method": request.method,
            "route": getattr(route, "path", "unmatched"), "status_code": response.status_code,
            "duration_ms": round((perf_counter() - start) * 1000, 2),
        })
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", responses={503: {"model": Readiness}})
    async def ready(response: Response) -> Readiness:
        report = await check_readiness(app.state.probes, settings.health_timeout_seconds)
        if report.status != "ready":
            response.status_code = 503
        return report

    return app


if __name__ == "__main__":
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000,
                log_config=logging_config(settings.log_level), access_log=False)
