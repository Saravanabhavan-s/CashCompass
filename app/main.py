import app.models  # noqa: F401 — registers ORM append-only + VaultProtected guards on import

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import egress, hurdles, ledger, sensitive, state, vault
from app.exceptions import (
    CleartextLeakError,
    DecryptionError,
    EgressPolicyViolation,
    KeyConfigurationError,
    NormalizationError,
    ShreddedError,
    TokenNotFoundError,
)
from app.vault.keys import get_key_provider


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail at boot, not at the first request, if vault keys aren't configured.
    get_key_provider()
    yield


app = FastAPI(
    title="CashCompass",
    description="Decision-aware personal finance foundation — Layer 1 + Layer 2 (PII vault)",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(hurdles.router)
app.include_router(ledger.router)
app.include_router(state.router)
app.include_router(sensitive.router)
app.include_router(vault.router)
app.include_router(egress.router)


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


# Centralized handlers — replaces repeating app/api/ledger.py's try/except
# pattern at every new call site. Handler bodies for DecryptionError and
# CleartextLeakError deliberately do NOT echo str(exc): those messages can
# carry field-level context that should not reach a client response.
@app.exception_handler(ShreddedError)
async def _shredded_handler(request: Request, exc: ShreddedError):
    return _error_response(410, str(exc))


@app.exception_handler(TokenNotFoundError)
async def _not_found_handler(request: Request, exc: TokenNotFoundError):
    return _error_response(404, str(exc))


@app.exception_handler(NormalizationError)
async def _normalization_handler(request: Request, exc: NormalizationError):
    return _error_response(422, str(exc))


@app.exception_handler(KeyConfigurationError)
async def _key_config_handler(request: Request, exc: KeyConfigurationError):
    return _error_response(503, "Vault key configuration error.")


@app.exception_handler(DecryptionError)
async def _decryption_handler(request: Request, exc: DecryptionError):
    return _error_response(500, "Decryption failed.")


@app.exception_handler(CleartextLeakError)
async def _cleartext_leak_handler(request: Request, exc: CleartextLeakError):
    return _error_response(500, "Internal vault protection error.")


@app.exception_handler(EgressPolicyViolation)
async def _egress_violation_handler(request: Request, exc: EgressPolicyViolation):
    return _error_response(500, "Egress policy violation.")
