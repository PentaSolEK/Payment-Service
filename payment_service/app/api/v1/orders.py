from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.schemas.schemas import OrderResponse
from app.services.payment_service import PaymentService, OrderNotFound

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=list[OrderResponse])
async def list_orders(db: AsyncSession = Depends(get_db)):
    """Список всех заказов."""
    service = PaymentService(db)
    orders = await service.list_orders()
    return orders


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(order_id: int, db: AsyncSession = Depends(get_db)):
    """Получить заказ по ID."""
    service = PaymentService(db)
    try:
        return await service.get_order(order_id)
    except OrderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
