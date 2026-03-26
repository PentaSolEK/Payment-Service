"""
Payment Service — точка входа в приложение FastAPI."""
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI

from app.db.database import create_tables, AsyncSessionLocal
from app.api.v1 import orders, payments


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    await _seed_orders()
    yield


async def _seed_orders():
    from sqlalchemy import select, func
    from app.models.order import Order

    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(Order))
        if count == 0:
            orders_data = [
                Order(amount=Decimal("1500.00"), description="Office supplies"),
                Order(amount=Decimal("750.50"), description="IT equipment"),
                Order(amount=Decimal("200.00"), description="Catering"),
            ]
            db.add_all(orders_data)
            await db.commit()


app = FastAPI(
    title="Payment Service",
    description=(
        "RESTful-сервис для управления платежами по заказам "
        "Поддерживает наличные и платежи через банковский API."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(orders.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
