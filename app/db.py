from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import settings

engine = create_engine(settings.database_url, echo=False)


class Base(DeclarativeBase):
    pass


def get_session():
    with Session(engine) as session:
        yield session
