from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

RegistrationRole = Literal["rider", "driver"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: Name
    password: Password
    role: RegistrationRole = "rider"


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserResponse


class Coordinate(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class RideCreateRequest(BaseModel):
    pickup: Coordinate
    dropoff: Coordinate


class FareEstimateResponse(BaseModel):
    distance_meters: int
    fare_paise: int
    currency: Literal["INR"]
    fare_base_paise: int
    fare_per_km_paise: int


class RideResponse(BaseModel):
    id: UUID
    rider_id: UUID
    driver_id: UUID | None
    driver_name: str | None
    driver_vehicle: str | None
    status: str
    pickup: Coordinate
    dropoff: Coordinate
    distance_meters: int
    fare_paise: int
    currency: Literal["INR"]
    fare_base_paise: int
    fare_per_km_paise: int
    request_deadline: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class RideStatusHistoryResponse(BaseModel):
    id: UUID
    previous_status: str | None
    new_status: str
    actor_id: UUID | None
    reason: str | None
    created_at: datetime


class RideHistoryResponse(BaseModel):
    ride: RideResponse
    status_history: list[RideStatusHistoryResponse]


class DriverLocationUpdate(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class DriverLocationResponse(DriverLocationUpdate):
    updated_at: datetime


class DriverAvailabilityUpdate(BaseModel):
    online: bool


class DriverAvailabilityResponse(BaseModel):
    online: bool
    availability: str


class RideOfferResponse(BaseModel):
    attempt_id: UUID
    attempt_number: int
    expires_at: datetime
    expires_in_seconds: int
    ride: RideResponse


class RideTransitionRequest(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = (
        None
    )


class AdminRideCancellationRequest(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    confirm_driver_release: bool = False


class DriverVerificationRequest(BaseModel):
    verified: bool
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ride_id: UUID
    user_id: UUID
    amount_paise: int
    currency: Literal["INR"]
    status: str
    reference_id: str
    attempt_count: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    succeeded_at: datetime | None


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class AdminUserActivationRequest(BaseModel):
    is_active: bool
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AdminDriverResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    is_active: bool
    is_online: bool
    availability: str
    verification_status: str


class DriverSummaryResponse(BaseModel):
    completed_rides: int
    gross_fare_paise: int
    currency: Literal["INR"]
