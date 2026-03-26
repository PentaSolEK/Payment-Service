"""
Модель заказа.

С точки зрения платежного сервиса, заказ является неизменяемой сущностью —
он никогда не создается и не изменяется через API этого сервиса.
Сервис только считывает заказы и изменяет их payment_status
в ответ на платежные операции.
"""
import enum
from decimal import Decimal

from sqlalchemy import Numeric, String, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class PaymentStatus(str, enum.Enum):
    UNPAID = "unpaid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus), default=PaymentStatus.UNPAID, nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=True)

    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        "Payment", back_populates="order", lazy="selectin"
    )

    def recalculate_status(self, payments: list | None = None) -> None:
        """
        Пересчитать и обновить payment_status.

        Передать явный список платежей, чтобы избежать зависимости от актуальности коллекции отношений ORM.
        В случае отсутствия списка используется self.payments.
        """
        from app.models.payment import PaymentOperationStatus
        source = payments if payments is not None else self.payments
        paid_total = sum(
            p.amount for p in source if p.status == PaymentOperationStatus.DEPOSITED
        )
        if paid_total <= 0:
            self.payment_status = PaymentStatus.UNPAID
        elif paid_total >= self.amount:
            self.payment_status = PaymentStatus.PAID
        else:
            self.payment_status = PaymentStatus.PARTIALLY_PAID

    def __repr__(self) -> str:
        return f"<Order id={self.id} amount={self.amount} status={self.payment_status}>"
