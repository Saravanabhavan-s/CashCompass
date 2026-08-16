import app.models  # noqa: F401 — registers ORM append-only guard on import

from fastapi import FastAPI

from app.api import hurdles, ledger, sensitive, state

app = FastAPI(
    title="CashCompass",
    description="Decision-aware personal finance foundation — Layer 1",
    version="0.1.0",
)

app.include_router(hurdles.router)
app.include_router(ledger.router)
app.include_router(state.router)
app.include_router(sensitive.router)
