import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cards import REQUIRED_DETAILS, build_card
from app.intake import ExtractedOrder, IntakeAgent, IntakeError, normalized_draft
from app.main import app
from app.models import BatchOrder, Order
from app.service import BatchSendError, OrderConflict, preview_batch, submit_batch
from app.settings import Settings
from app.trello import TrelloClient, TrelloError


def config(tmp_path):
    return Settings(
        "key-for-test",
        "token-for-test",
        {"digital": "digital-board", "sublimacao": "sub-board"},
        frozenset(),
        tmp_path / "test.db",
    )


def order_data():
    return {
        "order_id": "MIXED-001",
        "customer_name": "Aurora ficticia",
        "customer_contact": "Ana / DEMO",
        "reference_at": "2026-10-15T15:59:00-03:00",
        "artwork_path": r"\\demo\artes\pedido.pdf",
        "items": [
            {"item_id": "1", "product_code": "dtf_uv", "quantity": 100, "dimensions": "10 x 15 cm"},
            {"item_id": "2", "product_code": "windflag", "quantity": 2, "dimensions": ""},
        ],
    }


class FakeBoard:
    """A local REST fake that keeps copied descriptions for cross-link verification."""

    def __init__(self, fail_post=None, uncertain=False, missing_sub_template=False):
        self.calls = []
        self.cards = {}
        self.posts = []
        self.fail_post = fail_post
        self.uncertain = uncertain
        self.missing_sub_template = missing_sub_template

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path.removeprefix("/1/")
        if request.method == "GET" and path.startswith("boards/"):
            board = path.split("/")[1]
            name = "LANÇAMENTO PEDIDOS" if board == "digital-board" else "LANÇAMENTO DO PEDIDO"
            return httpx.Response(
                200,
                json=[
                    {"id": board + "-entry", "name": name},
                    {"id": board + "-draft", "name": "ENGATILHADOS"},
                    {"id": board + "-models", "name": "CARTÕES MODELO"},
                ],
            )
        if request.method == "GET" and path.startswith("lists/"):
            digital = "digital-board" in path
            cards = (
                []
                if not digital and self.missing_sub_template
                else [
                    {
                        "id": "uv-template" if digital else "flag-template",
                        "name": "DTF UV" if digital else "WINDFLAG",
                        "desc": "Instruções originais do modelo.",
                        "closed": False,
                    }
                ]
            )
            return httpx.Response(200, json=cards)
        if request.method == "POST" and path == "cards":
            payload = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            self.posts.append(payload)
            if len(self.posts) == self.fail_post:
                if self.uncertain:
                    raise httpx.ReadTimeout("May have succeeded", request=request)
                return httpx.Response(429, json={"error": "rate limit"})
            card_id = f"created-{len(self.cards) + 1}"
            self.cards[card_id] = {
                "id": card_id,
                "url": "https://trello.com/c/" + card_id,
                "idBoard": "digital-board"
                if payload["idList"].startswith("digital")
                else "sub-board",
                "desc": payload["desc"],
                "idList": payload["idList"],
                "name": payload["name"],
                "due": payload.get("due"),
                "dueComplete": False,
                "closed": False,
            }
            return httpx.Response(200, json=self.cards[card_id])
        if path.startswith("cards/"):
            card = self.cards[path.split("/")[1]]
            if request.method == "PUT":
                if request.headers.get("content-type", "").startswith("application/json"):
                    card.update(json.loads(request.content))
                else:
                    card["desc"] = parse_qs(request.content.decode())["desc"][0]
            return httpx.Response(200, json=card)
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.mark.parametrize("field", list(REQUIRED_DETAILS))
def test_each_missing_required_detail_is_a_draft_without_a_deadline(field):
    data = order_data()
    item = data.pop("items")[0]
    single = {**data, **{key: value for key, value in item.items() if key != "item_id"}}
    single[field] = None if field in {"quantity", "reference_at"} else "   "
    preview = build_card(Order.model_validate(single))
    assert preview.status == "draft"
    assert preview.list_name == "ENGATILHADOS"
    assert preview.deadline is None
    assert preview.missing_fields == [REQUIRED_DETAILS[field]]
    assert "Entrega prevista:" not in preview.description
    assert f"- [ ] {REQUIRED_DETAILS[field]}" in preview.description


def test_mixed_preview_keeps_shared_id_and_routes_each_product(tmp_path):
    preview = preview_batch(BatchOrder.model_validate(order_data()), config(tmp_path))
    ready, draft = preview["cards"]
    assert (preview["ready"], preview["drafts"]) == (1, 1)
    assert (ready["board_key"], ready["template_name"], ready["list_name"]) == (
        "digital",
        "DTF UV",
        "LANÇAMENTO PEDIDOS",
    )
    assert ready["deadline"]["due_at"].startswith("2026-10-16T18:00")
    assert (draft["board_key"], draft["template_name"], draft["list_name"]) == (
        "sublimacao",
        "WINDFLAG",
        "ENGATILHADOS",
    )
    assert draft["deadline"] is None
    assert ready["group_id"] == draft["group_id"] == "MIXED-001"
    assert ready["order_id"] != draft["order_id"]


def test_batch_copies_templates_omits_draft_due_and_links_only_its_cards(tmp_path):
    fake = FakeBoard()
    client = TrelloClient(config(tmp_path), transport=httpx.MockTransport(fake.handle))
    batch = BatchOrder.model_validate(order_data())
    try:
        result = submit_batch(batch, config(tmp_path), client)
        assert result["status"] == "sent"
        assert fake.posts[0]["idCardSource"] == "uv-template"
        assert fake.posts[0]["due"] == "2026-10-16T21:00:00+00:00"
        assert fake.posts[1]["idCardSource"] == "flag-template"
        assert fake.posts[1]["idList"] == "sub-board-draft"
        assert "due" not in fake.posts[1]
        assert all(
            "Instruções originais do modelo." in card["desc"] for card in fake.cards.values()
        )
        assert all(
            "https://trello.com/c/created-1" in card["desc"]
            and "https://trello.com/c/created-2" in card["desc"]
            for card in fake.cards.values()
        )
        assert len([call for call in fake.calls if call.method == "PUT"]) == 2
        assert submit_batch(batch, config(tmp_path), client)["duplicate"] is True
        assert len(fake.posts) == 2
        with pytest.raises(OrderConflict):
            changed = {**order_data(), "customer_name": "Another customer"}
            submit_batch(BatchOrder.model_validate(changed), config(tmp_path), client)
    finally:
        client.close()


def test_every_target_is_checked_before_any_card_is_created(tmp_path):
    fake = FakeBoard(missing_sub_template=True)
    client = TrelloClient(config(tmp_path), transport=httpx.MockTransport(fake.handle))
    try:
        with pytest.raises(BatchSendError) as error:
            submit_batch(BatchOrder.model_validate(order_data()), config(tmp_path), client)
        assert error.value.retryable is True
        assert fake.posts == []
    finally:
        client.close()


@pytest.mark.parametrize("failed_first", [False, True])
def test_composite_item_ids_never_reuse_a_different_orders_card(tmp_path, failed_first):
    fake = FakeBoard(fail_post=1 if failed_first else None)
    settings = config(tmp_path)
    client = TrelloClient(settings, transport=httpx.MockTransport(fake.handle))
    first = BatchOrder.model_validate(
        {
            **order_data(),
            "order_id": "PAIR__A",
            "items": [{**order_data()["items"][0], "item_id": "B"}],
        }
    )
    second = BatchOrder.model_validate(
        {
            **order_data(),
            "order_id": "PAIR",
            "items": [{**order_data()["items"][0], "item_id": "A__B"}],
        }
    )
    try:
        if failed_first:
            with pytest.raises(BatchSendError):
                submit_batch(first, settings, client)
        else:
            submit_batch(first, settings, client)
        with pytest.raises(BatchSendError, match="identificador deste item"):
            submit_batch(second, settings, client)
        assert len(fake.posts) == 1
        if not failed_first:
            assert "ESTAMPI_GROUP_ID=PAIR__A" in fake.cards["created-1"]["desc"]
    finally:
        client.close()


def test_known_partial_failure_resumes_only_the_remaining_item(tmp_path):
    fake = FakeBoard(fail_post=2)
    client = TrelloClient(config(tmp_path), transport=httpx.MockTransport(fake.handle))
    batch = BatchOrder.model_validate(order_data())
    try:
        with pytest.raises(BatchSendError) as error:
            submit_batch(batch, config(tmp_path), client)
        assert error.value.result["status"] == "partial"
        assert len(error.value.result["cards"]) == 1
        assert error.value.retryable
        result = submit_batch(batch, config(tmp_path), client)
        assert result["status"] == "sent"
        assert result["cards"][0]["duplicate"] is True
        assert len(fake.cards) == 2
        assert [post["idCardSource"] for post in fake.posts] == [
            "uv-template",
            "flag-template",
            "flag-template",
        ]
    finally:
        client.close()


def test_uncertain_batch_post_is_never_blindly_repeated(tmp_path):
    fake = FakeBoard(fail_post=2, uncertain=True)
    client = TrelloClient(config(tmp_path), transport=httpx.MockTransport(fake.handle))
    batch = BatchOrder.model_validate(order_data())
    try:
        with pytest.raises(BatchSendError) as error:
            submit_batch(batch, config(tmp_path), client)
        assert error.value.result["status"] == "unknown"
        assert len(error.value.result["cards"]) == 1
        assert error.value.retryable is False
        with pytest.raises(OrderConflict):
            submit_batch(batch, config(tmp_path), client)
        assert len(fake.posts) == 2
    finally:
        client.close()


@pytest.mark.parametrize("tamper", ["board", "marker"])
def test_related_links_refuse_cards_outside_this_test_order(tmp_path, tamper):
    fake = FakeBoard()
    client = TrelloClient(config(tmp_path), transport=httpx.MockTransport(fake.handle))
    try:
        result = submit_batch(BatchOrder.model_validate(order_data()), config(tmp_path), client)
        if tamper == "board":
            fake.cards["created-1"]["idBoard"] = "unconfigured-production-board"
        else:
            fake.cards["created-1"]["desc"] = fake.cards["created-1"]["desc"].replace(
                "ESTAMPI_GROUP_ID=MIXED-001\n", "ESTAMPI_GROUP_ID=MIXED-001OTHER\n"
            )
        fake.calls.clear()
        with pytest.raises(TrelloError):
            client.link_order_items(result["cards"], "MIXED-001")
        assert not any(call.method == "PUT" for call in fake.calls)
    finally:
        client.close()


def extracted_data(**changes):
    return {
        "order_id": None,
        "customer_name": "Aurora ficticia",
        "customer_contact": None,
        "artwork_path": None,
        "reference_at": None,
        "notes": None,
        "items": [
            {
                "product_code": "windflag",
                "raw_product": "2 windflags",
                "quantity": 2,
                "dimensions": None,
                "artwork_path": None,
                "reference_at": None,
                "notes": None,
            }
        ],
        "warnings": [],
        **changes,
    }


def ai_response(data):
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(data)}]}
        ],
    }


def test_openai_extracts_to_reviewable_draft_without_inventing_missing_fields(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=ai_response(extracted_data()))

    agent = IntakeAgent(
        replace(config(tmp_path), openai_api_key="fake-openai-key"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = agent.extract("Aurora ficticia quer 2 windflags, medidas ainda não informadas.")
        payload = json.loads(requests[0].content)
        assert requests[0].url == "https://api.openai.com/v1/responses"
        assert requests[0].headers["Authorization"] == "Bearer fake-openai-key"
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        assert payload["text"]["format"]["schema"]["additionalProperties"] is False
        assert "tools" not in payload
        assert result["draft"]["order_id"] == ""
        assert result["draft"]["items"][0]["dimensions"] == ""
        assert result["draft"]["reference_at"] is None
        assert result["draft"]["customer_contact"] == ""
        assert "fake-openai-key" not in json.dumps(result)
    finally:
        agent.close()


def test_unknown_product_remains_visible_for_manual_selection():
    data = extracted_data()
    data["items"][0].update(product_code=None, raw_product="Produto fora do catálogo")
    draft = normalized_draft(ExtractedOrder.model_validate(data))
    assert draft["items"][0]["product_code"] is None
    assert draft["items"][0]["raw_product"] == "Produto fora do catálogo"
    with pytest.raises(ValueError):
        BatchOrder.model_validate(
            {
                **draft,
                "order_id": "DEMO",
                "items": [{k: v for k, v in draft["items"][0].items() if k != "raw_product"}],
            }
        )


@pytest.mark.parametrize(
    "response_data,status",
    [
        ({"status": "incomplete", "output": []}, 502),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            },
            422,
        ),
        (ai_response({"unexpected": True}), 502),
        (ai_response(extracted_data(items=[])), 422),
    ],
)
def test_bad_ai_output_never_becomes_a_submittable_order(tmp_path, response_data, status):
    agent = IntakeAgent(
        replace(config(tmp_path), openai_api_key="fake-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_data)),
    )
    try:
        with pytest.raises(IntakeError) as error:
            agent.extract("some text")
        assert error.value.status_code == status
    finally:
        agent.close()


def test_missing_ai_key_leaves_manual_preview_available(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: config(tmp_path))
    calls = []
    agent = IntakeAgent(
        config(tmp_path), transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    try:
        with pytest.raises(IntakeError) as error:
            agent.extract("2 windflags")
        assert error.value.status_code == 503
        assert calls == []
    finally:
        agent.close()
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["ai_configured"] is False
        assert "openai_api_key" not in health
        assert client.post("/orders/batch/preview", json=order_data()).status_code == 200
        assert client.post("/intake/parse", json={"text": "  "}).status_code == 422
