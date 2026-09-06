"""Independent ASGI entry points for the Mini App and the private admin site."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
import dashboard


@asynccontextmanager
async def lifespan(app):
    await dashboard.startup()
    try:
        yield
    finally:
        await dashboard.dao.aclose()
        if dashboard.db_manager.pool:
            await dashboard.db_manager.pool.close()


def create_app(miniapp: bool):
    app = FastAPI(title='TU UGMK Mini App' if miniapp else 'TU UGMK Admin',
                  lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    for route in dashboard.app.routes:
        if isinstance(route, APIRoute):
            public_app_route = route.path == '/webapp' or route.path.startswith('/api/')
            if public_app_route == miniapp:
                app.router.routes.append(route)

    @app.get('/healthz')
    async def health():
        await dashboard.dao.ping()
        async with dashboard.db_manager.pool.acquire() as conn:
            await conn.fetchval('SELECT 1')
        return {'status': 'ok', 'service': 'miniapp' if miniapp else 'dashboard'}

    if miniapp:
        @app.get('/')
        async def home():
            return RedirectResponse('/webapp')
    return app


miniapp_app = create_app(True)
admin_app = create_app(False)
