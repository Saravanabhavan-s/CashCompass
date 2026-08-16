from sqlalchemy.orm import Session

from app.models.hurdle import Hurdle
from app.schemas.hurdle import HurdleCreate
from app.schemas.primitives import Primitive


def decompose(raw_description: str) -> list[Primitive]:
    """
    Decompose a raw life-event description into universal financial primitives.

    Signature defined here as the interface seam. Layer 2+ (LLM layer) will
    implement NL decomposition. Manual primitive entry is used for now.
    """
    raise NotImplementedError(
        "decompose() requires the LLM layer (Layer 2+). "
        "Provide primitives manually via POST /hurdles."
    )


def create_hurdle(db: Session, data: HurdleCreate) -> Hurdle:
    hurdle = Hurdle(
        label=data.label,
        raw_description=data.raw_description,
        primitives=[p.model_dump(mode="json") for p in data.primitives],
    )
    db.add(hurdle)
    db.commit()
    db.refresh(hurdle)
    return hurdle


def get_hurdle(db: Session, hurdle_id: str) -> Hurdle | None:
    return db.get(Hurdle, hurdle_id)
