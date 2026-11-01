
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: str
    display_name: str


class PhoneCredentialUpdate(BaseModel):
    """A customer's phone number and phone PIN (M7.2). The PIN is a SecretStr,
    so it never appears in a repr, a log line or an error message."""

    model_config = ConfigDict(extra="forbid")

    phone_number: str = Field(min_length=8, max_length=20, description="E.164, e.g. +15555550100")
    pin: SecretStr = Field(description="6 digits; not one digit repeated or a straight run")
