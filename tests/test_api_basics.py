def test_health_and_stateless_readiness_do_not_require_database(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["dependencies"]["database"] == "optional_not_checked"


def test_request_id_is_returned(client):
    response = client.get("/health", headers={"X-Request-ID": "frontend-123"})
    assert response.headers["X-Request-ID"] == "frontend-123"


def test_optional_bearer_auth(monkeypatch, client):
    monkeypatch.setenv("BASKET_API_KEY", "test-only-secret")

    denied = client.post("/basket/compare", json={"ingredients": ["milk"]})
    assert denied.status_code == 401

    with monkeypatch.context() as context:
        context.setattr("app.main.compare_basket", lambda ingredients: [])
        allowed = client.post(
            "/basket/compare",
            json={"ingredients": ["milk"]},
            headers={"Authorization": "Bearer test-only-secret"},
        )
    assert allowed.status_code == 200


def test_basket_input_is_bounded(client):
    response = client.post(
        "/basket/compare",
        json={"ingredients": [f"ingredient-{index}" for index in range(51)]},
    )
    assert response.status_code == 422
