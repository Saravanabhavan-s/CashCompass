from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/cashcompass"

    # --- Layer 2: PII vault ---
    default_tenant_id: str = "default"
    vault_key_source: Literal["env", "keyring", "kms"] = "env"
    # JSON map of {"<version>": "<base64 32-byte key>"}. Every KEK version ever
    # used must stay here until rotate_kek confirms no row still needs it.
    vault_keks: SecretStr = SecretStr("")
    vault_kek_version: int = 1  # which version NEW wraps use
    vault_hsk: SecretStr = SecretStr("")  # base64, 32 bytes
    vault_admin_key: SecretStr = SecretStr("")  # shared-secret prototype auth
    vault_detokenize_allowed_callers: str = ""  # comma-separated allowlist

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
