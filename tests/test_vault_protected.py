import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy.exc import InternalError

from app.exceptions import CleartextLeakError
from app.schemas.sensitive import Dependent, SensitiveProfileUpsert
from app.services import sensitive as sensitive_svc
from app.vault.protected import VaultProtectedModel, VaultToken


class _Schema(BaseModel):
    name_token: VaultToken


def test_pydantic_rejects_cleartext_in_vault_token_field():
    with pytest.raises(ValidationError):
        _Schema(name_token="Ramya G")


def test_pydantic_accepts_a_well_formed_token():
    obj = _Schema(name_token="TKN_NAME_8f3a1c2b4d5e6f70")
    assert obj.name_token == "TKN_NAME_8f3a1c2b4d5e6f70"


class _ReadModel(VaultProtectedModel):
    __vault_protected_fields__ = frozenset({"name_token"})
    name_token: str


def test_model_serializer_blocks_model_construct_bypass():
    # model_construct() skips field validators entirely — this is the bypass
    # the wrap serializer exists to close. Pydantic wraps errors raised
    # inside a @model_serializer in PydanticSerializationError; our
    # CleartextLeakError message survives inside it.
    obj = _ReadModel.model_construct(name_token="Ramya G — plaintext smuggled in")
    with pytest.raises(PydanticSerializationError, match="vault token"):
        obj.model_dump()


def test_model_serializer_allows_a_well_formed_token_through():
    obj = _ReadModel.model_construct(name_token="TKN_NAME_8f3a1c2b4d5e6f70")
    dumped = obj.model_dump()
    assert dumped["name_token"] == "TKN_NAME_8f3a1c2b4d5e6f70"


def test_orm_flush_rejects_cleartext_sensitive_profile(db_session, vault_keys):
    from app.models.sensitive import SensitiveProfile

    profile = SensitiveProfile(
        user_id="orm-guard-test",
        medical_conditions=["plaintext diagnosis, not a token"],
        vault_protected=True,
    )
    db_session.add(profile)
    with pytest.raises(CleartextLeakError):
        db_session.flush()
    db_session.rollback()


def test_orm_flush_allows_a_not_yet_migrated_row(db_session, vault_keys):
    # vault_protected=False is the escape hatch that makes the flip
    # incremental — a legacy row isn't blocked by the guard.
    from app.models.sensitive import SensitiveProfile

    profile = SensitiveProfile(
        user_id="legacy-row-test",
        medical_conditions=["still plaintext, not yet migrated"],
        vault_protected=False,
    )
    db_session.add(profile)
    db_session.flush()  # does not raise
    db_session.rollback()


def test_db_trigger_rejects_raw_sql_cleartext_insert(db_session, vault_keys):
    from sqlalchemy import text

    with pytest.raises(InternalError, match="vault-protected"):
        db_session.execute(
            text(
                "INSERT INTO sensitive_profile "
                "(id, user_id, medical_conditions, sensitivity_tier, vault_protected) "
                "VALUES ('raw-sql-test', 'raw-sql-user', :mc, 'TIER_HEALTH_FAMILY', true)"
            ),
            {"mc": '["plaintext, not a token"]'},
        )
        db_session.flush()
    db_session.rollback()


def test_service_layer_produces_only_valid_tokens_end_to_end(db_session, vault_keys):
    # Belt-and-suspenders: the normal service path never trips the guard.
    data = SensitiveProfileUpsert(user_id="guard-happy-path", medical_conditions=["flu"], dependents=[Dependent(relation="child", age=5, notes="mild allergy")])
    profile = sensitive_svc.upsert_sensitive_profile(db_session, data)
    assert profile.vault_protected is True
