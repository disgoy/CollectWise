
from pydantic import BaseModel, ConfigDict, Field


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: str
    display_name: str
