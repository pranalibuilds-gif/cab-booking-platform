from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentOutcome:
    succeeded: bool
    failure_code: str | None = None


class PaymentProviderError(Exception):
    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


class PaymentProvider(Protocol):
    def charge(self, reference_id: str, amount_paise: int, currency: str) -> PaymentOutcome: ...


class LocalMockPaymentProvider:
    """Development-only simulated checkout; it never handles card data."""

    def charge(self, reference_id: str, amount_paise: int, currency: str) -> PaymentOutcome:
        if not reference_id or amount_paise < 0 or currency != "INR":
            raise PaymentProviderError("invalid_payment_request")
        return PaymentOutcome(succeeded=True)
