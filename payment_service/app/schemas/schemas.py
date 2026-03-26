from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.order import PaymentStatus
from app.models.payment import PaymentType, PaymentOperationStatus, BankPaymentStatus


# ── Order ────────────────────────────────────────────────────────────────────

class OrderResponse(BaseModel):
    id: int
    amount: Decimal
    payment_status: PaymentStatus
    description: str | None

    model_config = {"from_attributes": True}


# ── Payment ──────────────────────────────────────────────────────────────────

class PaymentCreate(BaseModel):
    order_id: int
    amount: Decimal = Field(gt=0, description="Payment amount, must be positive")
    payment_type: PaymentType

    @model_validator(mode="after")
    def amount_precision(self):
        # Limit to 2 decimal places
        if self.amount != round(self.amount, 2):
            raise ValueError("Amount must have at most 2 decimal places.")
        return self


class PaymentResponse(BaseModel):
    id: int
    order_id: int
    amount: Decimal
    payment_type: PaymentType
    status: PaymentOperationStatus
    created_at: datetime
    updated_at: datetime
    bank_payment_id: str | None = None
    bank_status: BankPaymentStatus | None = None
    bank_paid_at: datetime | None = None

    model_config = {"from_attributes": True}


class PaymentRefundResponse(BaseModel):
    payment: PaymentResponse
    order: OrderResponse


class PaymentDepositResponse(BaseModel):
    payment: PaymentResponse
    order: OrderResponse


# ── Bank API ─────────────────────────────────────────────────────────────────

class BankAcquiringStartRequest(BaseModel):
    order_id: int
    amount: Decimal


class BankAcquiringStartResponse(BaseModel):
    bank_payment_id: str


class BankAcquiringCheckResponse(BaseModel):
    bank_payment_id: str
    amount: Decimal
    status: BankPaymentStatus
    paid_at: datetime | None = None


# ── Errors ───────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str
