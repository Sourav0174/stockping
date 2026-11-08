from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routes.api import router as api_router
from app.routes.embed import router as embed_router
from app.routes.webhooks import router as webhook_router
from app.services.worker import worker

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
ASSETS_DIR = os.path.join(STATIC_DIR, "assets")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables and unique partial indexes
    await init_db()
    # Start the managed background outbox worker
    worker.start()
    try:
        yield
    finally:
        # Gracefully shut down background worker
        await worker.stop()


app = FastAPI(
    title="StockPing",
    description="Reliable back-in-stock notification engine",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    if path == "/embed" or path.startswith("/assets/"):
        # Specifically permit embedding inside third-party store iframes
        response.headers["Content-Security-Policy"] = "frame-ancestors *;"
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
    else:
        # Retain strict framing protection for API and webhook routes
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self';"

    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# Mount static assets if compiled
if os.path.exists(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# Include routers
app.include_router(api_router)
app.include_router(embed_router)
app.include_router(webhook_router)


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "StockPing"}
