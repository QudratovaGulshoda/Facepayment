from pydantic import BaseModel, Field, field_validator

PHONE_PATTERN = r"^\+998\d{9}$"


class CaptureIn(BaseModel):
    challenge_id: str
    frames: list[str] = Field(..., description="base64 JPEG kadrlar", max_length=40)
    timestamps_ms: list[int] = Field(..., max_length=40)


class EnrollIn(CaptureIn):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    full_name: str = Field(..., min_length=3, max_length=120)
    pin: str = Field(..., pattern=r"^\d{4,6}$")
    consent: bool

    @field_validator("pin")
    @classmethod
    def weak_pin(cls, v: str) -> str:
        if len(set(v)) == 1 or v in "0123456789" or v in "9876543210":
            raise ValueError("PIN juda oddiy")
        return v


class VariantIn(CaptureIn):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    pin: str = Field(..., pattern=r"^\d{4,6}$")
    label: str = Field("makiyaj", max_length=32)


class DeleteIn(CaptureIn):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    pin: str = Field(..., pattern=r"^\d{4,6}$")


class PaymentIn(CaptureIn):
    idempotency_key: str = Field(..., min_length=8, max_length=64)
    amount: int = Field(..., gt=0, le=100_000_000)


class PinIn(BaseModel):
    pin: str = Field(..., pattern=r"^\d{4,6}$")


class MerchantIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)


class TerminalIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    merchant_id: str | None = None
    public_key_b64: str = Field(..., min_length=40, max_length=64)
    role: str = Field("payment", pattern=r"^(payment|enroll)$")


class TopUpIn(BaseModel):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    amount: int = Field(..., gt=0, le=100_000_000)


class CardAddIn(BaseModel):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    pin: str = Field(..., pattern=r"^\d{4,6}$")
    number: str = Field(..., min_length=16, max_length=19)
    expire: str = Field(..., pattern=r"^\d{4}$", description="MMYY")


class CardVerifyIn(BaseModel):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    code: str = Field(..., pattern=r"^\d{4,6}$")


class PhonePinIn(BaseModel):
    phone: str = Field(..., pattern=PHONE_PATTERN)
    pin: str = Field(..., pattern=r"^\d{4,6}$")
