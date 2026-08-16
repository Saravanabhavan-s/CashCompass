from datetime import datetime

from pydantic import BaseModel

from app.schemas.primitives import Primitive


class HurdleCreate(BaseModel):
    label: str
    raw_description: str
    primitives: list[Primitive]


class HurdleRead(BaseModel):
    id: str
    label: str
    raw_description: str
    primitives: list[Primitive]
    created_at: datetime

    model_config = {"from_attributes": True}
