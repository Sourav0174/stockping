from app.routes.admin import router as admin_router
from app.routes.api import router as api_router
from app.routes.embed import router as embed_router
from app.routes.webhooks import router as webhook_router

__all__ = ["admin_router", "api_router", "embed_router", "webhook_router"]
