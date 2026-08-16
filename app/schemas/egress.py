from pydantic import BaseModel

from app.enums import EgressDestination


class EgressPreviewResponse(BaseModel):
    destination: EgressDestination
    payload: dict
    leaks: list[str]
