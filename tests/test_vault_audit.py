import pytest

from app.enums import VaultFieldType
from app.exceptions import AppendOnlyViolation, TokenNotFoundError, UnauthorizedVaultAccess
from app.models.vault import DetokenizationAudit
from app.services import vault as vault_svc


def test_every_detokenize_writes_exactly_one_audit_row(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    before = db_session.query(DetokenizationAudit).count()
    vault_svc.detokenize(db_session, token, caller="test-caller", purpose="unit test")
    after = db_session.query(DetokenizationAudit).count()
    assert after == before + 1


def test_denied_call_is_audited_with_a_reason(db_session, vault_keys):
    with pytest.raises(UnauthorizedVaultAccess):
        vault_svc.detokenize(db_session, "TKN_NAME_0000000000000000", caller="not-allowed", purpose="x")

    row = (
        db_session.query(DetokenizationAudit)
        .filter_by(caller="not-allowed")
        .order_by(DetokenizationAudit.requested_at.desc())
        .first()
    )
    assert row is not None
    assert row.granted is False
    assert row.denial_reason


def test_missing_token_denial_is_audited(db_session, vault_keys):
    with pytest.raises(TokenNotFoundError):
        vault_svc.detokenize(db_session, "TKN_NAME_0000000000000000", caller="test-caller", purpose="x")

    row = (
        db_session.query(DetokenizationAudit)
        .filter_by(token_id="TKN_NAME_0000000000000000")
        .one()
    )
    assert row.granted is False
    assert "not found" in row.denial_reason


def test_orm_update_blocked_by_append_only_guard(db_session, vault_keys):
    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.detokenize(db_session, token, caller="test-caller", purpose="setup")

    audit_row = db_session.query(DetokenizationAudit).filter_by(token_id=token).one()
    audit_row.purpose = "tampered"
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        db_session.flush()
    db_session.rollback()


def test_db_delete_blocked_by_postgres_trigger(db_session, vault_keys):
    from sqlalchemy import text
    from sqlalchemy.exc import InternalError

    token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, "Ramya G")
    vault_svc.detokenize(db_session, token, caller="test-caller", purpose="setup")

    with pytest.raises(InternalError, match="append-only"):
        db_session.execute(text("DELETE FROM detokenization_audit WHERE token_id = :t"), {"t": token})
        db_session.flush()
    db_session.rollback()


def test_audit_pagination(db_session, vault_keys):
    for i in range(5):
        token = vault_svc.tokenize_field(db_session, VaultFieldType.NAME, f"Person {i}")
        vault_svc.detokenize(db_session, token, caller="test-caller", purpose=f"page test {i}")

    page1, total = vault_svc.list_audit(db_session, limit=2, offset=0)
    page2, total2 = vault_svc.list_audit(db_session, limit=2, offset=2)
    assert total == total2 == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r.id for r in page1}.isdisjoint({r.id for r in page2})
