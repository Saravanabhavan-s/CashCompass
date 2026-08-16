def test_tokenize_requires_admin_key(client):
    resp = client.post("/vault/tokenize", json={"field_type": "NAME", "value": "Ramya G"})
    assert resp.status_code == 401


def test_wrong_admin_key_rejected(client):
    resp = client.post(
        "/vault/tokenize",
        json={"field_type": "NAME", "value": "Ramya G"},
        headers={"X-Vault-Admin-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_tokenize_with_admin_key_succeeds(client, admin_headers):
    resp = client.post(
        "/vault/tokenize", json={"field_type": "NAME", "value": "Ramya G"}, headers=admin_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_id"].startswith("TKN_NAME_")


def test_tokenize_then_detokenize_round_trip(client, admin_headers):
    tok_resp = client.post(
        "/vault/tokenize", json={"field_type": "PAN", "value": "ABCDE1234F"}, headers=admin_headers
    )
    token_id = tok_resp.json()["token_id"]

    detok_resp = client.post(
        "/vault/detokenize",
        json={"token_id": token_id, "caller": "test-caller", "purpose": "api round trip test"},
    )
    assert detok_resp.status_code == 200
    assert detok_resp.json()["value"] == "ABCDE1234F"


def test_detokenize_requires_caller_and_purpose(client):
    resp = client.post("/vault/detokenize", json={"token_id": "TKN_NAME_0000000000000000", "caller": "", "purpose": ""})
    assert resp.status_code == 422


def test_detokenize_denies_unlisted_caller(client, admin_headers):
    tok_resp = client.post(
        "/vault/tokenize", json={"field_type": "NAME", "value": "Someone"}, headers=admin_headers
    )
    token_id = tok_resp.json()["token_id"]

    resp = client.post(
        "/vault/detokenize", json={"token_id": token_id, "caller": "unlisted-caller", "purpose": "x"}
    )
    assert resp.status_code == 403


def test_shred_requires_reason(client, admin_headers):
    tok_resp = client.post(
        "/vault/tokenize", json={"field_type": "NAME", "value": "To Be Shredded"}, headers=admin_headers
    )
    token_id = tok_resp.json()["token_id"]

    resp = client.post(f"/vault/shred/{token_id}", json={"reason": ""}, headers=admin_headers)
    assert resp.status_code == 422


def test_shred_then_detokenize_returns_410(client, admin_headers):
    tok_resp = client.post(
        "/vault/tokenize", json={"field_type": "NAME", "value": "To Be Shredded 2"}, headers=admin_headers
    )
    token_id = tok_resp.json()["token_id"]

    shred_resp = client.post(f"/vault/shred/{token_id}", json={"reason": "test"}, headers=admin_headers)
    assert shred_resp.status_code == 200

    detok_resp = client.post(
        "/vault/detokenize", json={"token_id": token_id, "caller": "test-caller", "purpose": "x"}
    )
    assert detok_resp.status_code == 410


def test_rotate_kek_endpoint_accepts_version_not_key_material(client, admin_headers):
    resp = client.post("/vault/rotate-kek", json={"new_version": 5}, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["to_version"] == 5
    assert "new_kek" not in body


def test_audit_endpoint_paginates(client, admin_headers):
    for i in range(3):
        tok_resp = client.post(
            "/vault/tokenize", json={"field_type": "NAME", "value": f"Audited {i}"}, headers=admin_headers
        )
        client.post(
            "/vault/detokenize",
            json={"token_id": tok_resp.json()["token_id"], "caller": "test-caller", "purpose": "audit page test"},
        )

    resp = client.get("/vault/audit", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 3


def test_egress_preview_debate_agent_reports_no_leaks(client):
    resp = client.get("/egress/preview", params={"destination": "DEBATE_AGENT"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["destination"] == "DEBATE_AGENT"
    assert body["leaks"] == []


def test_egress_preview_optimizer_no_admin_required(client):
    resp = client.get("/egress/preview", params={"destination": "OPTIMIZER"})
    assert resp.status_code == 200


def test_egress_preview_user_facing_requires_admin(client):
    resp = client.get(
        "/egress/preview", params={"destination": "USER_FACING", "caller": "test-caller", "purpose": "x"}
    )
    assert resp.status_code == 401


def test_egress_preview_user_facing_requires_caller_and_purpose(client, admin_headers):
    resp = client.get("/egress/preview", params={"destination": "USER_FACING"}, headers=admin_headers)
    assert resp.status_code == 422
