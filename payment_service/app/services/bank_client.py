"""
Клиент банковского API.

Решения по проектированию:
- Тонкий слой адаптера: остальная часть приложения взаимодействует только с
исключениями домена (BankAPIError, BankPaymentNotFound), а не с необработанными
HTTP-ошибками. Это изолирует внешнюю зависимость.

- Повторные попытки с экспоненциальной задержкой для временных сетевых ошибок / ошибок 5xx..
"""
import asyncio
import logging
from decimal import Decimal

import httpx

from app.config import settings
from app.schemas.schemas import BankAcquiringStartResponse, BankAcquiringCheckResponse

logger = logging.getLogger(__name__)


class BankAPIError(Exception):
    """Генерируется, когда банк возвращает ошибку бизнес-логики."""


class BankPaymentNotFound(BankAPIError):
    """Эта ошибка возникает, когда банк сообщает, что идентификатор платежа неизвестен."""


class BankAPIClient:
    """
    Асинхронный HTTP-клиент для bank.api.

    Повторные попытки применяются только к сбоям на сетевом уровне и ответам с кодом 5xx
    (временные ошибки). Ответы с кодом 4xx считаются окончательными и не подвергаются повторным попыткам.
    """

    def __init__(
        self,
        base_url: str = settings.bank_api_base_url,
        timeout: float = settings.bank_api_timeout,
        retries: int = settings.bank_api_retries,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = retries

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None

        for attempt in range(1, self._retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload)

                if response.status_code < 500:
                    # Non-retryable: business logic or client error
                    return self._unwrap(response)

                # 5xx — log and retry
                logger.warning(
                    "Bank API %s returned %s (attempt %d/%d)",
                    path, response.status_code, attempt, self._retries,
                )
                last_exc = BankAPIError(f"Bank API server error: {response.status_code}")

            except httpx.TransportError as exc:
                logger.warning(
                    "Bank API %s transport error (attempt %d/%d): %s",
                    path, attempt, self._retries, exc,
                )
                last_exc = BankAPIError(f"Bank API unreachable: {exc}")

            if attempt < self._retries:
                await asyncio.sleep(0.5 * attempt)  # simple exponential back-off

        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except Exception:
            raise BankAPIError(f"Non-JSON response from bank API: {response.text[:200]}")

        if response.status_code == 200:
            return data

        # The spec says failures are returned as a string error
        error_msg = data if isinstance(data, str) else data.get("error", str(data))
        if "not found" in error_msg.lower():
            raise BankPaymentNotFound(error_msg)
        raise BankAPIError(error_msg)

    async def acquiring_start(self, order_id: int, amount: Decimal) -> BankAcquiringStartResponse:
        """
        Вызывает метод bank.api/acquiring_start.
        В случае успеха возвращает уникальный идентификатор платежа банка.
        """
        payload = {"order_id": order_id, "amount": str(amount)}
        data = await self._post("acquiring_start", payload)
        return BankAcquiringStartResponse(bank_payment_id=data["bank_payment_id"])

    async def acquiring_check(self, bank_payment_id: str) -> BankAcquiringCheckResponse:
        """
        Вызывает bank.api/acquiring_check.
        Возвращает текущее состояние платежа на стороне банка.
        """
        payload = {"bank_payment_id": bank_payment_id}
        data = await self._post("acquiring_check", payload)
        return BankAcquiringCheckResponse(**data)



default_bank_client = BankAPIClient()
