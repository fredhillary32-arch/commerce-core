from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.modules.addresses.router import router as addresses_router
from app.modules.auth.router import router as auth_router
from app.modules.cart.router import router as cart_router
from app.modules.catalog.router import (
    categories_router,
    products_router,
    public_router,
    variants_router,
)
from app.modules.inventory.router import router as inventory_router
from app.modules.orders.router import (
    admin_orders_router,
    checkout_router,
    orders_router,
)
from app.modules.shipping.router import router as shipping_router
from app.modules.tax.router import router as tax_router
from app.modules.users.router import roles_router, users_router


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(title=settings.app_name, debug=settings.debug)

    register_exception_handlers(app)

    app.include_router(health_router, prefix=settings.api_v1_prefix)
    app.include_router(auth_router, prefix=settings.api_v1_prefix)
    app.include_router(users_router, prefix=settings.api_v1_prefix)
    app.include_router(roles_router, prefix=settings.api_v1_prefix)
    app.include_router(public_router, prefix=settings.api_v1_prefix)
    app.include_router(products_router, prefix=settings.api_v1_prefix)
    app.include_router(variants_router, prefix=settings.api_v1_prefix)
    app.include_router(categories_router, prefix=settings.api_v1_prefix)
    app.include_router(cart_router, prefix=settings.api_v1_prefix)
    app.include_router(inventory_router, prefix=settings.api_v1_prefix)
    app.include_router(addresses_router, prefix=settings.api_v1_prefix)
    app.include_router(tax_router, prefix=settings.api_v1_prefix)
    app.include_router(shipping_router, prefix=settings.api_v1_prefix)
    app.include_router(checkout_router, prefix=settings.api_v1_prefix)
    app.include_router(orders_router, prefix=settings.api_v1_prefix)
    app.include_router(admin_orders_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
