from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.schemas.schemas import (
    PaymentCreate,
    PaymentResponse,
    PaymentDepositResponse,
    PaymentRefundResponse,
)
from app.services.payment_service import (
    PaymentService,
    PaymentError,
    OrderNotFound,
    PaymentNotFound,
)
from app.models.payment import PaymentType

router = APIRouter(prefix="/payments", tags=["payments"])


def _get_service(db: AsyncSession = Depends(get_db)) -> PaymentService:
    return PaymentService(db)


@router.get("/order/{order_id}", response_model=list[PaymentResponse])
async def list_payments(order_id: int, service: PaymentService = Depends(_get_service)):
    """Список всех оплат для данного заказа."""
    try:
        return await service.list_payments(order_id)
    except OrderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/", response_model=PaymentDepositResponse, status_code=status.HTTP_201_CREATED)
async def create_and_deposit_payment(
    payload: PaymentCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Создает платеж и немедленно вносит его.

    Для наличных: создайте и внесите в одной транзакции.
    Для эквайринга: создайте, вызовите bank.api/acquiring_start, затем bank.api/acquiring_check.
    Если банк подтвердит платеж, он будет немедленно зачислен.
    """
    service = PaymentService(db)
    try:
        payment = await service.create_payment(
            order_id=payload.order_id,
            amount=payload.amount,
            payment_type=payload.payment_type,
        )

        if payload.payment_type == PaymentType.CASH:
            payment = await service.deposit_cash(payment.id)
        else:
            payment = await service.acquire_and_deposit(payment.id)

        await db.commit()
        await db.refresh(payment)
        await db.refresh(payment.order)
        return PaymentDepositResponse(payment=payment, order=payment.order)

    except (OrderNotFound, PaymentNotFound) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except PaymentError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{payment_id}/refund", response_model=PaymentRefundResponse)
async def refund_payment(
    payment_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Возврат внесенного платежа.

    Для обработки платежей сначала связываются с банком, чтобы узнать актуальное состояние счета.
    Возврат средств на  счете возможен только в том случае, если банк аннулировал/вернул платеж.
    """
    service = PaymentService(db)
    try:
        payment = await service.refund_payment(payment_id)
        await db.commit()
        await db.refresh(payment)
        await db.refresh(payment.order)
        return PaymentRefundResponse(payment=payment, order=payment.order)

    except PaymentNotFound as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except PaymentError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{payment_id}/sync", response_model=PaymentResponse)
async def sync_acquiring_payment(
    payment_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Запуск синхронизации платежа с банком вручную.

    Полезно, когда банк неожиданно меняет состояние платежа (например, аннулирует его).
    """
    service = PaymentService(db)
    try:
        payment = await service.sync_acquiring_payment(payment_id)
        await db.commit()
        await db.refresh(payment)
        return payment
    except (PaymentNotFound, PaymentError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
