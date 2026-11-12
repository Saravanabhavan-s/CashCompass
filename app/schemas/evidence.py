from datetime import datetime

from pydantic import BaseModel


class EvidenceItemRead(BaseModel):
    id: str
    source: str
    query: str
    lens_tags: list[str]
    content: str
    citation: str
    fetched_at: datetime

    model_config = {"from_attributes": True}


class EvidenceSnapshotRead(BaseModel):
    id: str
    hurdle_id: str
    snapshot_hash: str
    question_count: int
    item_count: int
    source_versions: dict
    created_at: datetime
    items: list[EvidenceItemRead]

    model_config = {"from_attributes": True}
