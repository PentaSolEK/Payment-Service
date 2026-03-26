"""
Модель платежей.
Решение по проектированию: наследование по одной таблице (STI).
Все типы платежей используют одну и ту же схему.
STI удовлетворяет этому требованию, используя одну таблицу + столбец-дискриминатор `payment_type`,
при этом допуская поведение, специфичное для каждого типа в подклассах Python.

Состояние банка для платежей эквайринга хранится в той же строке через столбцы, допускающие значение null .

"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class PaymentType(str, enum.Enum):
    CASH = "cash"
    ACQUIRING = "acquiring"


class PaymentOperationStatus(str, enum.Enum):
    """Внутренний жизненный цикл платежа."""
    PENDING = "pending"       # created, not yet confirmed
    DEPOSITED = "deposited"   # successfully deposited / paid
    REFUNDED = "refunded"     # fully refunded


class BankPaymentStatus(str, enum.Enum):
    """Зеркальное отображение статусов, возвращаемых API банка."""
    CREATED = "created"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Payment(Base):
    """
    Единая модель оплаты. Все строки с типами платежей находятся в этой таблице.
    Поведение, специфичное для каждого типа, обрабатывается в подклассах Python через STI.
    """
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    payment_type: Mapped[PaymentType] = mapped_column(
        SAEnum(PaymentType), nullable=False
    )
    status: Mapped[PaymentOperationStatus] = mapped_column(
        SAEnum(PaymentOperationStatus),
        default=PaymentOperationStatus.PENDING,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # --- Acquiring-only fields (NULL for cash payments) ---
    bank_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    bank_status: Mapped[BankPaymentStatus | None] = mapped_column(
        SAEnum(BankPaymentStatus), nullable=True
    )
    bank_paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="payments")  # noqa: F821

    # --- STI discriminator ---
    __mapper_args__ = {
        "polymorphic_on": "payment_type",
        "polymorphic_identity": None,
    }

    def deposit(self, all_order_payments: list | None = None) -> None:
        """Отмечает платеж как поступивший и обновляет статус заказа."""
        if self.status != PaymentOperationStatus.PENDING:
            raise ValueError(f"Cannot deposit a payment in status '{self.status}'.")
        self.status = PaymentOperationStatus.DEPOSITED
        # Build the list the order will see after this deposit
        payments = list(all_order_payments) if all_order_payments is not None else list(self.order.payments)
        if self not in payments:
            payments.append(self)
        self.order.recalculate_status(payments)

    def refund(self, all_order_payments: list | None = None) -> None:
        """Отмечает платеж как возвращенный и обновляет статус заказа."""
        if self.status != PaymentOperationStatus.DEPOSITED:
            raise ValueError(f"Cannot refund a payment in status '{self.status}'.")
        self.status = PaymentOperationStatus.REFUNDED
        payments = list(all_order_payments) if all_order_payments is not None else list(self.order.payments)
        if self not in payments:
            payments.append(self)
        self.order.recalculate_status(payments)

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id} type={self.payment_type} "
            f"amount={self.amount} status={self.status}>"
        )


class CashPayment(Payment):
    """Оплата наличными — без использования внешнего API."""
    __mapper_args__ = {"polymorphic_identity": PaymentType.CASH}


class AcquiringPayment(Payment):
    """Банковское эквайринговое платежное обслуживание — синхронизировано с bank.api."""
    __mapper_args__ = {"polymorphic_identity": PaymentType.ACQUIRING}

    def deposit(self, all_order_payments: list | None = None) -> None:
        """
        Для обработки платежа подтверждение депозита происходит только после подтверждения банком.
        Банковский статус должен быть подтвержден до того, как мы примем депозит.
        """
        if self.bank_status != BankPaymentStatus.CONFIRMED:
            raise ValueError(
                f"Cannot deposit acquiring payment: bank status is '{self.bank_status}', "
                "expected 'confirmed'."
            )
        super().deposit(all_order_payments)

    def refund(self, all_order_payments: list | None = None) -> None:
        """
        Для осуществления возврата средств также требуется аннулирование платежа на стороне банка.
        Для принятия возврата средств статус банковского счета должен быть REFUNDED/CANCELLED.
        """
        if self.bank_status not in (BankPaymentStatus.CANCELLED, BankPaymentStatus.REFUNDED):
            raise ValueError(
                f"Cannot refund acquiring payment: bank status is '{self.bank_status}'."
            )
        super().refund(all_order_payments)
