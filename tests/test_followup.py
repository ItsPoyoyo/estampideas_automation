import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from test_batch_intake import FakeBoard, ai_response, config, order_data

from app import demo, followup, main
from app.followup import complete_item, preview_completion
from app.intake import IntakeAgent
from app.models import BatchOrder, CompletionRequest
from app.service import OrderConflict, get_batch, submit_batch, sync_batch
from app.storage import open_database
from app.trello import TrelloClient, TrelloError


def completion(revision=1, **changes):
    return CompletionRequest.model_validate({"revision": revision, "changes": changes})


@pytest.fixture
def workflow(tmp_path):
    settings = config(tmp_path)
    fake = FakeBoard()
    client = TrelloClient(settings, transport=httpx.MockTransport(fake.handle))
    batch = BatchOrder.model_validate(order_data())
    result = submit_batch(batch, settings, client)
    try:
        yield settings, fake, client, batch, result
    finally:
        client.close()


def test_completion_updates_same_card_preserves_links_and_applies_cutoff(workflow, monkeypatch):
    settings, fake, client, batch, created = workflow
    monkeypatch.setattr(followup, "utc_now", lambda: datetime(2026, 10, 15, 19, tzinfo=UTC))
    assert created["cards"][1]["verification"]["confirmed"]
    old_id = created["cards"][1]["card"]["id"]
    template_and_links = fake.cards[old_id]["desc"].split("### Instruções do modelo", 1)[1]
    result = complete_item(
        batch.order_id, "2", completion(dimensions="2,70 x 0,70 m"), settings, client
    )
    updated = result["cards"][1]
    assert updated["card"]["id"] == old_id
    assert updated["revision"] == 2
    assert updated["preview"]["list_name"] == "LANÇAMENTO DO PEDIDO"
    assert updated["verification"]["confirmed"]
    assert updated["preview"]["deadline"]["cutoff_applied"] is True
    assert updated["preview"]["deadline"]["due_at"].startswith("2026-10-27T18:00")
    assert "Informações completas" in updated["preview"]["deadline"]["explanation"]
    assert updated["details"]["reference_at"].startswith("2026-10-15T15:59")
    assert template_and_links in fake.cards[old_id]["desc"]
    assert fake.cards[old_id]["idList"] == "sub-board-entry"
    assert len(fake.posts) == 2
    assert any(event["kind"] == "card_released" for event in result["activities"])
    # Replaying the ORIGINAL request returns the current card, even after completion.
    replay = submit_batch(batch, settings, client)
    assert replay["duplicate"]
    assert replay["cards"][1]["preview"]["status"] == "ready"
    assert replay["cards"][1]["card"]["id"] == old_id
    assert len(fake.posts) == 2


def test_identical_completion_retry_does_not_repeat_put(workflow):
    settings, fake, client, batch, _ = workflow
    request = completion(dimensions="2,70 x 0,70 m")
    complete_item(batch.order_id, "2", request, settings, client)
    put_count = sum(call.method == "PUT" for call in fake.calls)
    result = complete_item(batch.order_id, "2", request, settings, client)
    assert result["duplicate"]
    assert sum(call.method == "PUT" for call in fake.calls) == put_count
    with pytest.raises(OrderConflict):
        complete_item(batch.order_id, "2", completion(dimensions="3 x 1 m"), settings, client)


def test_partial_completion_stays_pending_until_every_required_fact_arrives(tmp_path):
    settings = config(tmp_path)
    fake = FakeBoard()
    client = TrelloClient(settings, transport=httpx.MockTransport(fake.handle))
    batch = BatchOrder.model_validate({**order_data(), "customer_contact": ""})
    try:
        submit_batch(batch, settings, client)
        first = complete_item(
            batch.order_id, "2", completion(dimensions="2,70 x 0,70 m"), settings, client
        )
        assert first["cards"][1]["preview"]["status"] == "draft"
        assert fake.cards["created-2"]["due"] is None
        second = complete_item(
            batch.order_id, "2", completion(revision=2, customer_contact="Ana"), settings, client
        )
        assert second["cards"][1]["preview"]["status"] == "ready"
        assert second["cards"][0]["preview"]["status"] == "draft"
        assert len(fake.posts) == 2
    finally:
        client.close()


def test_reply_cannot_overwrite_existing_facts_or_change_product(workflow):
    settings, fake, client, batch, _ = workflow
    before = len(fake.calls)
    with pytest.raises(OrderConflict):
        complete_item(
            batch.order_id, "2", completion(dimensions="2 x 1 m", quantity=999), settings, client
        )
    with pytest.raises(ValueError):
        completion(product_code="dtf_uv")
    assert len(fake.calls) == before


@pytest.mark.parametrize("tamper", ["board", "column", "description", "completed", "deadline"])
def test_completion_preserves_external_trello_changes(workflow, tamper):
    settings, fake, client, batch, _ = workflow
    card = fake.cards["created-2"]
    if tamper == "board":
        card["idBoard"] = "unconfigured-production-board"
    elif tamper == "column":
        card["idList"] = "already-in-production"
    elif tamper == "description":
        card["desc"] = "Manually edited\n" + card["desc"]
    elif tamper == "deadline":
        card["due"] = "2026-12-01T18:00:00-03:00"
    else:
        card["dueComplete"] = True
    fake.calls.clear()
    with pytest.raises(TrelloError) as error:
        complete_item(batch.order_id, "2", completion(dimensions="2 x 1 m"), settings, client)
    assert error.value.status_code == 409
    assert not any(call.method in {"POST", "PUT"} for call in fake.calls)


def test_timed_out_put_is_reconciled_without_another_write(tmp_path):
    settings = config(tmp_path)
    fake = FakeBoard()
    faults = []

    def handler(request):
        response = fake.handle(request)
        if (
            request.method == "PUT"
            and request.headers.get("content-type", "").startswith("application/json")
            and not faults
        ):
            faults.append(True)
            raise httpx.ReadTimeout("Applied but reply was lost", request=request)
        return response

    client = TrelloClient(settings, transport=httpx.MockTransport(handler))
    batch = BatchOrder.model_validate(order_data())
    request = completion(dimensions="2 x 1 m")
    try:
        submit_batch(batch, settings, client)
        with pytest.raises(TrelloError):
            complete_item(batch.order_id, "2", request, settings, client)
        puts = sum(call.method == "PUT" for call in fake.calls)
        recovered = complete_item(batch.order_id, "2", request, settings, client)
        assert recovered["cards"][1]["revision"] == 2
        assert recovered["cards"][1]["verification"]["confirmed"]
        assert sum(call.method == "PUT" for call in fake.calls) == puts
        assert len(fake.posts) == 2
    finally:
        client.close()


def test_preview_has_no_trello_effects(workflow):
    settings, fake, _, batch, _ = workflow
    before = len(fake.calls)
    preview = preview_completion(batch.order_id, "2", completion(dimensions="2 x 1 m"), settings)
    assert preview["preview"]["status"] == "ready"
    assert len(fake.calls) == before


def test_second_partial_reply_can_be_recovered_after_a_lost_response(tmp_path):
    settings = config(tmp_path)
    fake = FakeBoard()
    fail_next_update = False

    def handler(request):
        nonlocal fail_next_update
        response = fake.handle(request)
        if fail_next_update and request.method == "PUT":
            fail_next_update = False
            raise httpx.ReadTimeout("Applied but reply was lost", request=request)
        return response

    client = TrelloClient(settings, transport=httpx.MockTransport(handler))
    batch = BatchOrder.model_validate({**order_data(), "customer_contact": ""})
    try:
        submit_batch(batch, settings, client)
        complete_item(batch.order_id, "2", completion(dimensions="2 x 1 m"), settings, client)
        fail_next_update = True
        with pytest.raises(TrelloError):
            complete_item(
                batch.order_id,
                "2",
                completion(revision=2, customer_contact="Ana"),
                settings,
                client,
            )
        pending = get_batch(batch.order_id, settings)["cards"][1]["pending_update"]
        assert pending == {
            "revision": 2,
            "state": "unknown",
            "changes": {"customer_contact": "Ana"},
        }
        recovered = complete_item(
            batch.order_id,
            "2",
            completion(revision=pending["revision"], **pending["changes"]),
            settings,
            client,
        )
        assert recovered["cards"][1]["revision"] == 3
        assert "pending_update" not in recovered["cards"][1]
        assert recovered["cards"][1]["verification"]["confirmed"]
        assert len(fake.posts) == 2
    finally:
        client.close()


def test_failed_confirmation_read_never_unlocks_another_creation(tmp_path):
    settings = config(tmp_path)
    fake = FakeBoard()

    def handler(request):
        if request.method == "GET" and "/cards/" in request.url.path:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return fake.handle(request)

    client = TrelloClient(settings, transport=httpx.MockTransport(handler))
    batch = BatchOrder.model_validate(order_data())
    try:
        result = submit_batch(batch, settings, client)
        assert result["status"] == "sent"
        assert all(item["verification"]["status"] == "unavailable" for item in result["cards"])
        assert submit_batch(batch, settings, client)["duplicate"]
        assert len(fake.posts) == 2
    finally:
        client.close()


def test_completion_endpoints_return_current_state_and_reject_stale_edits(workflow, monkeypatch):
    settings, fake, _, batch, _ = workflow
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "TrelloClient",
        lambda config: TrelloClient(config, transport=httpx.MockTransport(fake.handle)),
    )
    path = f"/orders/batch/{batch.order_id}/items/2"
    request = {"revision": 1, "changes": {"dimensions": "2 x 1 m"}}
    with TestClient(main.app) as client:
        before = len(fake.calls)
        assert client.post(path + "/preview", json=request).status_code == 200
        assert len(fake.calls) == before
        saved = client.patch(path, json=request)
        assert saved.status_code == 200
        assert saved.json()["cards"][1]["revision"] == 2
        assert (
            client.get(f"/orders/batch/{batch.order_id}").json()["cards"][1]["details"][
                "dimensions"
            ]
            == "2 x 1 m"
        )
        assert client.patch(path, json=request).json()["duplicate"]
        assert (
            client.patch(
                path, json={"revision": 1, "changes": {"dimensions": "3 x 1 m"}}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                path, json={"revision": 2, "changes": {"product_code": "dtf_uv"}}
            ).status_code
            == 422
        )
        assert client.patch(path.replace("/2", "/missing"), json=request).status_code == 404
    assert len(fake.posts) == 2


def test_settings_representation_never_contains_credentials(tmp_path):
    settings = replace(
        config(tmp_path),
        oauth_client_secret="secret-oauth-value",
        access_token="secret-access-value",
        openai_api_key="secret-ai-value",
    )
    assert all(
        value not in repr(settings)
        for value in [
            settings.key,
            settings.token,
            settings.oauth_client_secret,
            settings.access_token,
            settings.openai_api_key,
        ]
    )


def test_verified_destination_reports_actual_external_position(workflow):
    settings, fake, client, batch, _ = workflow
    fake.cards["created-2"]["idList"] = "sub-board-entry"
    result = sync_batch(batch.order_id, settings, client)
    verification = result["cards"][1]["verification"]
    assert verification["confirmed"] is False
    assert verification["status"] == "different"
    assert verification["list_name"] == "LANÇAMENTO DO PEDIDO"
    assert any(event["kind"] == "trello_changed" for event in result["activities"])


def test_schema_upgrade_keeps_existing_orders(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE orders (order_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL,result TEXT)"
        )
        db.execute("INSERT INTO orders VALUES ('old','original-hash','sent','{}')")
        db.execute(
            "CREATE TABLE order_batches (order_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL,result TEXT)"
        )
    upgraded = open_database(path)
    try:
        assert upgraded.execute(
            "SELECT order_id,fingerprint,status,revision FROM orders"
        ).fetchone() == ("old", "original-hash", "sent", 1)
    finally:
        upgraded.close()


def test_ai_followup_extracts_only_missing_facts(workflow):
    settings, _, _, _, created = workflow
    from app.models import Order

    output = {
        "customer_name": None,
        "customer_contact": None,
        "quantity": 999,
        "dimensions": "2,70 x 0,70 m",
        "artwork_path": None,
        "reference_at": None,
        "warnings": [],
    }
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=ai_response(output))

    agent = IntakeAgent(
        replace(settings, openai_api_key="fake-key"), transport=httpx.MockTransport(handler)
    )
    try:
        result = agent.complete(
            "2,70 x 0,70 m", Order.model_validate(created["cards"][1]["details"]), ["dimensions"]
        )
        assert result["changes"] == {"dimensions": "2,70 x 0,70 m"}
        assert requests[0]["text"]["format"]["strict"] is True
        assert requests[0]["store"] is False
        assert "tools" not in requests[0]
    finally:
        agent.close()


def test_weekend_demo_still_shows_a_real_cutoff_difference(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "utc_now", lambda: datetime(2026, 10, 17, 12, tzinfo=UTC))
    monkeypatch.setattr(main, "get_settings", lambda: config(tmp_path))
    with TestClient(main.app) as client:
        response = client.get("/demo/cutoff")
        assert response.status_code == 200
        result = response.json()
        assert result["before"]["due_at"] != result["after"]["due_at"]
        assert result["after"]["cutoff_applied"]
        assert client.get("/demo/cutoff?product_code=unknown").status_code == 422
        assert client.get("/orders/batch/unknown").status_code == 404
