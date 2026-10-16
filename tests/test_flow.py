from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cards import build_card
from app.deadlines import calculate_deadline
from app.main import app
from app.models import Order
from app.service import OrderConflict, submit_order
from app.settings import Settings
from app.trello import TrelloClient, TrelloError


def sample(**changes):
    return Order(
        **{
            "order_id": "DEMO-001",
            "customer_name": "Aurora Studio",
            "product_code": "dtf_uv",
            "quantity": 100,
            "dimensions": "10 x 15 cm",
            "customer_contact": "Ana / MZ-001",
            "artwork_path": r"\\servidor\artes\pedido.pdf",
            "reference_at": "2026-10-15T15:59:00-03:00",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "reference,expected,cutoff",
    [
        ("2026-10-15T15:59:00-03:00", "2026-10-16", False),
        ("2026-10-15T16:00:00-03:00", "2026-10-19", True),
        ("2026-10-16T15:59:00-03:00", "2026-10-19", False),
        ("2026-10-16T16:01:00-03:00", "2026-10-20", True),
        ("2026-10-17T10:00:00-03:00", "2026-10-20", False),
        ("2026-10-15T19:00:00Z", "2026-10-19", True),
    ],
)
def test_cutoff_and_weekends(reference, expected, cutoff):
    result = calculate_deadline(datetime.fromisoformat(reference), 1)
    assert result.due_at.date().isoformat() == expected
    assert result.cutoff_applied == cutoff
    assert result.due_at.hour == 18
    assert result.due_at.utcoffset().total_seconds() == -10800


def test_holiday_is_skipped():
    result = calculate_deadline(sample().reference_at, 1, frozenset({date(2026, 10, 16)}))
    assert result.due_at.date() == date(2026, 10, 19)


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        calculate_deadline(datetime(2026, 10, 15, 10), 1)


def test_product_routing_and_template():
    digital = build_card(sample())
    sub = build_card(sample(product_code="windflag", quantity=2))
    assert digital.board_key == "digital"
    assert digital.template_name == "DTF UV"
    assert digital.title == "DEMO-001 - AURORA STUDIO - 100 UND"
    assert sub.board_key == "sublimacao"
    assert sub.list_name == "LANÇAMENTO DO PEDIDO"
    assert sub.template_name == "WINDFLAG"
    assert sub.deadline.business_days == 7


def test_validation_and_preview_do_not_need_trello():
    with TestClient(app) as client:
        payload = sample().model_dump(mode="json")
        assert client.post("/orders/preview", json=payload).status_code == 200
        assert client.post("/orders/validate", json={**payload, "quantity": 0}).status_code == 422
        assert (
            client.post("/orders/preview", json={**payload, "product_code": "unknown"}).status_code
            == 422
        )
        assert (
            client.post(
                "/orders/preview", json={**payload, "reference_at": "2026-10-15T10:00:00"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/orders/preview", json={**payload, "product_code": "dtf_textil", "quantity": 101}
            ).status_code
            == 422
        )
        assert client.post("/orders/preview", json={**payload, "quantity": 1.5}).status_code == 422


def settings(tmp_path):
    return Settings(
        "test-key",
        "test-token",
        {"digital": "digital-board", "sublimacao": "sub-board"},
        frozenset(),
        tmp_path / "orders.db",
    )


def transport_calls(calls, fail_post=False):
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/lists"):
            return httpx.Response(
                200,
                json=[
                    {"id": "entry", "name": "LANÇAMENTO PEDIDOS"},
                    {"id": "models", "name": "CARTÕES MODELO"},
                ],
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": "model-card", "name": "DTF UV", "desc": "Conferir arte e medidas."}],
            )
        if fail_post:
            raise httpx.ReadTimeout("The POST may have succeeded", request=request)
        return httpx.Response(200, json={"id": "created", "url": "https://trello.com/c/created"})

    return httpx.MockTransport(handler)


def test_real_api_payload_copies_template_and_sets_utc_due(tmp_path):
    calls = []
    config = settings(tmp_path)
    trello = TrelloClient(config, transport=transport_calls(calls))
    result = submit_order(sample(), config, trello)
    from urllib.parse import parse_qs

    payload = parse_qs(calls[-1].content.decode())
    assert payload["idCardSource"] == ["model-card"]
    assert payload["idList"] == ["entry"]
    assert payload["due"] == ["2026-10-16T21:00:00+00:00"]
    assert "checklists" in payload["keepFromSource"][0]
    assert "Conferir arte e medidas." in payload["desc"][0]
    assert all(request.url.params["token"] == "test-token" for request in calls)
    assert result["card"]["id"] == "created"
    assert submit_order(sample(), config, trello)["duplicate"] is True
    assert sum(request.method == "POST" for request in calls) == 1
    with pytest.raises(OrderConflict):
        submit_order(sample(quantity=99), config, trello)
    trello.close()


def test_timeout_never_blindly_creates_a_second_card(tmp_path):
    calls = []
    config = settings(tmp_path)
    trello = TrelloClient(config, transport=transport_calls(calls, fail_post=True))
    with pytest.raises(TrelloError):
        submit_order(sample(), config, trello)
    with pytest.raises(OrderConflict):
        submit_order(sample(), config, trello)
    assert sum(request.method == "POST" for request in calls) == 1
    trello.close()


def test_missing_template_can_be_fixed_and_retried(tmp_path):
    config = settings(tmp_path)
    calls = []
    bad = TrelloClient(
        config, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(TrelloError):
        submit_order(sample(), config, bad)
    good = TrelloClient(config, transport=transport_calls(calls))
    assert submit_order(sample(), config, good)["card"]["id"] == "created"
    bad.close()
    good.close()


def test_template_id_selects_correct_product_when_titles_are_identical(tmp_path):
    from dataclasses import replace

    config = replace(settings(tmp_path), template_ids={"DTF UV": "uv-template"})

    def handler(request):
        if request.url.path.endswith("/lists"):
            return httpx.Response(
                200,
                json=[
                    {"id": "entry", "name": "LANÇAMENTO PEDIDOS"},
                    {"id": "models", "name": "CARTÕES MODELO"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"id": "wrong-template", "name": "0000 - NOME - UND"},
                {"id": "uv-template", "name": "0000 - NOME - UND"},
            ],
        )

    client = TrelloClient(config, transport=httpx.MockTransport(handler))
    try:
        assert client.resolve_target(build_card(sample()))[1]["id"] == "uv-template"
        client.settings = replace(config, template_ids={"DTF UV": "template-on-another-board"})
        with pytest.raises(TrelloError):
            client.resolve_target(build_card(sample()))
    finally:
        client.close()


@pytest.mark.parametrize(
    "production_board",
    ["protected-digital-short", "protected-sub-short", "protected-digital-id", "protected-sub-id"],
)
def test_production_boards_are_rejected_before_any_request(tmp_path, production_board):
    from dataclasses import replace

    calls = []
    config = replace(
        settings(tmp_path),
        boards={"digital": production_board, "sublimacao": "test-board"},
        blocked_board_ids=frozenset({production_board}),
    )
    client = TrelloClient(config, transport=transport_calls(calls))
    try:
        with pytest.raises(TrelloError):
            client.resolve_target(build_card(sample()))
        assert calls == []
    finally:
        client.close()


def test_protected_board_ids_are_loaded_from_local_configuration(tmp_path, monkeypatch):
    from app import settings as settings_module

    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    monkeypatch.delenv("TRELLO_BLOCKED_BOARD_IDS", raising=False)
    (tmp_path / ".env").write_text(
        "TRELLO_BLOCKED_BOARD_IDS=protected-digital-id, protected-sub-id,\n", encoding="utf-8"
    )
    assert settings_module.get_settings().blocked_board_ids == frozenset(
        {"protected-digital-id", "protected-sub-id"}
    )
