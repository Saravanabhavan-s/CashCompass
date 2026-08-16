from datetime import datetime

from pydantic import BaseModel, Field

from app.enums import VaultFieldType


class TokenizeRequest(BaseModel):
    field_type: VaultFieldType
    value: str = Field(min_length=1)
    tenant_id: str | None = None


class TokenizeResponse(BaseModel):
    token_id: str
    field_type: VaultFieldType
    tenant_id: str


class DetokenizeRequest(BaseModel):
    token_id: str = Field(min_length=1)
    caller: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


class DetokenizeResponse(BaseModel):
    token_id: str
    value: str


class ShredRequest(BaseModel):
    reason: str = Field(min_length=1)


class ShredResponse(BaseModel):
    token_id: str
    shredded_at: datetime
    reason: str


class RotateKekRequest(BaseModel):
    # Never accepts raw key material — see app/api/vault.py's rotate_kek
    # route docstring. The key itself is resolved server-side via
    # KeyProvider so it never traverses a request body, access log, or
    # FastAPI validation error message.
    new_version: int


class RotateKekResponse(BaseModel):
    to_version: int
    rewrapped: int
    remaining_on_old: int


class AuditEntry(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    token_id: str
    tenant_id: str
    caller: str
    purpose: str
    requested_at: datetime
    granted: bool
    denial_reason: str | None


class AuditPage(BaseModel):
    items: list[AuditEntry]
    total: int
    limit: int
    offset: int
