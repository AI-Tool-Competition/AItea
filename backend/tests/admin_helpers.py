"""Explicit administrator identities for isolated API tests."""


def grant_admin(client, owner):
    # Queue tests create private, explicit administrator fixtures. Production
    # routes continue to require their real admin dependency.
    with client.app.state.service.db.transaction() as conn:
        conn.execute("UPDATE user_access SET is_admin=1,revision=revision+1 WHERE user_id=?", (owner,))


def login_admin(client):
    response = client.post("/api/tea/auth/demo", json={})
    assert response.status_code == 200
    grant_admin(client, response.json()["id"])
