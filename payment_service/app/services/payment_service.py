"""
PaymentService — основная бизнес-логика.

Функционал:
- Создание платежей (наличными или через эквайринг).
- Внесение платежей (отметка как оплаченных; для эквайринга сначала синхронизация с банком).
- Возврат платежей (для эквайринга сначала обращение в банк для отмены, затем синхронизация).
- Синхронизация состояния платежей через эквайринг с банком по запросу.
Сервис намеренно не связан с HTTP-протоколами и может вызываться из
REST-обработчиков, фоновых задач, скриптов CLI или тестов.
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.payment import (
    Payment,
    CashPayment,
    AcquiringPayment,
    PaymentType,
    PaymentOperationStatus,
    BankPaymentStatus,
)
from app.services.bank_client import BankAPIClient, BankAPIError, BankPaymentNotFound, default_bank_client

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Raised for business-logic violations."""


class OrderNotFound(PaymentError):
    pass


class PaymentNotFound(PaymentError):
    pass


class PaymentService:
    def __init__(self, db: AsyncSession, bank_client: BankAPIClient | None = None):
        self._db = db
        self._bank = bank_client or default_bank_client

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _get_order(self, order_id: int) -> Order:
        result = await self._db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order is None:
            raise OrderNotFound(f"Order {order_id} not found.")
        return order

    async def _get_payment(self, payment_id: int) -> Payment:
        result = await self._db.execute(select(Payment).where(Payment.id == payment_id))
        payment = result.scalar_one_or_none()
        if payment is None:
            raise PaymentNotFound(f"Payment {payment_id} not found.")
        return payment

    def _validate_payment_amount(self, order: Order, amount: Decimal) -> None:
        """Убедитесь, что сумма нового платежа не превысит сумму заказа."""
        deposited = sum(
            p.amount for p in order.payments
            if p.status == PaymentOperationStatus.DEPOSITED
        )
        pending = sum(
            p.amount for p in order.payments
            if p.status == PaymentOperationStatus.PENDING
        )
        if deposited + pending + amount > order.amount:
            raise PaymentError(
                f"Payment of {amount} would exceed order amount {order.amount}. "
                f"Already allocated: {deposited + pending}."
            )

    # ── Public operations ─────────────────────────────────────────────────────

    async def get_order(self, order_id: int) -> Order:
        return await self._get_order(order_id)

    async def list_orders(self) -> list[Order]:
        result = await self._db.execute(select(Order))
        return list(result.scalars().all())

    async def list_payments(self, order_id: int) -> list[Payment]:
        await self._get_order(order_id)  # verify order exists
        result = await self._db.execute(
            select(Payment).where(Payment.order_id == order_id)
        )
        return list(result.scalars().all())

    async def create_payment(self, order_id: int, amount: Decimal, payment_type: PaymentType) -> Payment:
        """
        Создайте новый платеж для заказа.

        Для наличных: создан в статусе PENDING — вызывающая сторона должна вызвать deposit() отдельно,
        или мы внесем средства немедленно.

        Для эквайринга: также в статусе PENDING; банк связывается с помощью acquire_and_deposit().
        """
        order = await self._get_order(order_id)

        from app.models.order import PaymentStatus
        if order.payment_status == PaymentStatus.PAID:
            raise PaymentError(f"Order {order_id} is already fully paid.")

        self._validate_payment_amount(order, amount)

        if payment_type == PaymentType.CASH:
            payment: Payment = CashPayment(order_id=order_id, amount=amount, payment_type=payment_type)
        else:
            payment = AcquiringPayment(order_id=order_id, amount=amount, payment_type=payment_type)

        self._db.add(payment)
        await self._db.flush()  # get payment.id without committing
        return payment

    async def _get_order_with_payments(self, order_id: int) -> tuple["Order", list["Payment"]]:
        """Load order and all its payments eagerly via explicit join."""
        from sqlalchemy import select as sa_select
        # Expire identity map so we always get fresh data from DB
        self._db.expire_all()
        result = await self._db.execute(sa_select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order is None:
            raise OrderNotFound(f"Order {order_id} not found.")
        # selectin relationship will have loaded payments
        return order, list(order.payments)

    async def deposit_cash(self, payment_id: int) -> Payment:
        """Внесение наличный платеж (переход ОЖИДАЕТСЯ → ВНЕСЕНО)"""
        payment = await self._get_payment(payment_id)
        if payment.payment_type != PaymentType.CASH:
            raise PaymentError("Use deposit_acquiring() for acquiring payments.")

        order, all_payments = await self._get_order_with_payments(payment.order_id)
        payment.order = order

        # Ensure this payment is included in the list
        ids = {p.id for p in all_payments}
        if payment.id not in ids:
            all_payments.append(payment)

        payment.deposit(all_payments)
        await self._db.flush()
        return payment

    async def acquire_and_deposit(self, payment_id: int) -> Payment:
        """
        Для обработки платежа через систему эквайринга:

        1. Вызываем функцию, чтобы инициировать платеж.

        2. Проверяем его статус.

        3. Если статус ПОДТВЕРЖДЕН, средства вносятся на локальный счет.

        """
        payment = await self._get_payment(payment_id)
        if payment.payment_type != PaymentType.ACQUIRING:
            raise PaymentError("Use deposit_cash() for cash payments.")
        if payment.status != PaymentOperationStatus.PENDING:
            raise PaymentError(f"Payment {payment_id} is already in status '{payment.status}'.")

        order, all_payments = await self._get_order_with_payments(payment.order_id)
        payment.order = order
        if payment.id not in {p.id for p in all_payments}:
            all_payments.append(payment)
        payment._all_payments = all_payments  # stored for use in _sync

        # Шаг 1: инициализация банка
        try:
            start_resp = await self._bank.acquiring_start(
                order_id=payment.order_id, amount=payment.amount
            )
        except BankAPIError as exc:
            raise PaymentError(f"Bank refused to start payment: {exc}") from exc

        payment.bank_payment_id = start_resp.bank_payment_id
        await self._db.flush()

        # Шаг 2: Проверка статуса (в реальной системе это может быть фоновой задачей).
        await self._sync_acquiring_from_bank(payment)

        return payment

    async def sync_acquiring_payment(self, payment_id: int) -> Payment:
        """
        Согласование статуса локального платежа с банком.

        Предназначено для вызова из:

        - Периодической фоновой задачи.

        - Обработчика веб-хуков.

        - Вручную через API.
        """
        payment = await self._get_payment(payment_id)
        if payment.payment_type != PaymentType.ACQUIRING:
            raise PaymentError("Only acquiring payments can be synchronised with the bank.")
        if not payment.bank_payment_id:
            raise PaymentError("Payment has no bank_payment_id — initiation may have failed.")

        order, all_payments = await self._get_order_with_payments(payment.order_id)
        payment.order = order
        if payment.id not in {p.id for p in all_payments}:
            all_payments.append(payment)
        payment._all_payments = all_payments

        await self._sync_acquiring_from_bank(payment)
        return payment

    async def _sync_acquiring_from_bank(self, payment: AcquiringPayment) -> None:
        """
        Получение актуального состояния от банка и его локальное применение.
        Корректная обработка неожиданных изменений на стороне банка.
        """
        try:
            check = await self._bank.acquiring_check(payment.bank_payment_id)
        except BankPaymentNotFound:
            logger.warning("Bank payment %s not found; leaving local status unchanged.", payment.bank_payment_id)
            return
        except BankAPIError as exc:
            logger.error("Bank check failed for %s: %s", payment.bank_payment_id, exc)
            return

        payment.bank_status = check.status
        payment.bank_paid_at = check.paid_at

        all_payments = getattr(payment, "_all_payments", None)

        if check.status == BankPaymentStatus.CONFIRMED and payment.status == PaymentOperationStatus.PENDING:
            try:
                payment.deposit(all_payments)
            except ValueError as exc:
                logger.error("Failed to deposit after bank confirmation: %s", exc)

        elif check.status in (BankPaymentStatus.CANCELLED, BankPaymentStatus.REFUNDED):
            if payment.status == PaymentOperationStatus.DEPOSITED:
                try:
                    payment.refund(all_payments)
                except ValueError as exc:
                    logger.error("Failed to refund after bank cancellation: %s", exc)

        await self._db.flush()

    async def refund_payment(self, payment_id: int) -> Payment:
        """
        Возврат платежа (ДЕПОЗИТ → ВОЗВРАЩЕН).

        Для эквайринга: сначала запускается синхронизация на стороне банка. Банк должен
        отменить/вернуть платеж, чтобы локальный возврат прошел успешно.
        """
        payment = await self._get_payment(payment_id)

        order, all_payments = await self._get_order_with_payments(payment.order_id)
        payment.order = order
        if payment.id not in {p.id for p in all_payments}:
            all_payments.append(payment)
        payment._all_payments = all_payments

        if payment.payment_type == PaymentType.ACQUIRING and payment.bank_payment_id:
            await self._sync_acquiring_from_bank(payment)
            if payment.status == PaymentOperationStatus.REFUNDED:
                await self._db.flush()
                return payment

        try:
            payment.refund(all_payments)
        except ValueError as exc:
            raise PaymentError(str(exc)) from exc

        await self._db.flush()
        return payment
