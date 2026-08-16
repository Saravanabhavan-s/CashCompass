from app.schemas.sensitive import Dependent, SensitiveProfileUpsert
from app.services import sensitive as svc
from app.vault.tokens import is_token


def test_upsert_stores_tokens_not_plaintext(db_session, vault_keys):
    data = SensitiveProfileUpsert(
        user_id="user-1",
        medical_conditions=["chemotherapy"],
        family_commitments=["caring for parent"],
        obligations=["monthly support payment"],
        dependents=[Dependent(relation="child", age=8, notes="asthma")],
    )
    profile = svc.upsert_sensitive_profile(db_session, data)

    assert all(is_token(c) for c in profile.medical_conditions)
    assert all(is_token(c) for c in profile.family_commitments)
    assert all(is_token(c) for c in profile.obligations)
    assert is_token(profile.dependents[0]["notes_token"])
    assert profile.dependents[0]["relation"] == "child"
    assert profile.dependents[0]["age"] == 8


def test_vault_protected_column_is_true_after_upsert(db_session, vault_keys):
    data = SensitiveProfileUpsert(user_id="user-2", medical_conditions=["flu"])
    profile = svc.upsert_sensitive_profile(db_session, data)
    assert profile.vault_protected is True


def test_sensitivity_tier_column_persisted(db_session, vault_keys):
    data = SensitiveProfileUpsert(user_id="user-3", medical_conditions=["flu"])
    profile = svc.upsert_sensitive_profile(db_session, data)
    assert profile.sensitivity_tier == "TIER_HEALTH_FAMILY"


def test_dependents_notes_tokenized_relation_and_age_cleartext(db_session, vault_keys):
    data = SensitiveProfileUpsert(
        user_id="user-4",
        dependents=[Dependent(relation="spouse", age=34, notes="chronic condition, private")],
    )
    profile = svc.upsert_sensitive_profile(db_session, data)
    dep = profile.dependents[0]
    assert dep["relation"] == "spouse"
    assert dep["age"] == 34
    assert is_token(dep["notes_token"])
    assert "chronic" not in str(dep)


def test_upsert_is_reusable_for_same_user(db_session, vault_keys):
    data1 = SensitiveProfileUpsert(user_id="user-5", medical_conditions=["flu"])
    svc.upsert_sensitive_profile(db_session, data1)

    data2 = SensitiveProfileUpsert(user_id="user-5", medical_conditions=["diabetes"])
    profile2 = svc.upsert_sensitive_profile(db_session, data2)

    from app.models.sensitive import SensitiveProfile

    count = db_session.query(SensitiveProfile).filter_by(user_id="user-5").count()
    assert count == 1
    assert is_token(profile2.medical_conditions[0])
