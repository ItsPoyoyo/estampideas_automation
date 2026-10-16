import json
import threading
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_batch_intake import FakeBoard, config, order_data

from app import main
from app.followup import complete_item
from app.models import BatchOrder, CompletionRequest
from app.production import ProductionWatcher
from app.production_rules import CHECKLIST_NAME, ROUTES
from app.service import OrderConflict, get_batch, submit_batch, sync_batch
from app.storage import open_database
from app.trello import TrelloClient


class ProductionBoard(FakeBoard):
    """REST fake with real checklist state and product-specific production columns."""

    def __init__(self):
        super().__init__()
        self.checklists = {}
        self.items_created = 0

    def handle(self, request):
        path = request.url.path.removeprefix("/1/")
        parts = path.split("/")
        if request.method == "GET" and path.startswith("boards/"):
            listing = super().handle(request).json()
            route = ROUTES["dtf_uv" if parts[1] == "digital-board" else "windflag"]
            return httpx.Response(
                200,
                json=listing
                + [
                    {"id": f"{parts[1]}-flow-{index}", "name": name}
                    for index, name in enumerate(route.lists[1:], 1)
                ],
            )
        if len(parts) == 3 and parts[0] == "cards" and parts[2] == "checklists":
            self.calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=self.checklists.get(parts[1], []))
            payload = json.loads(request.content)
            checklist = {"id": f"flow-{parts[1]}", "name": payload["name"], "checkItems": []}
            self.checklists.setdefault(parts[1], []).append(checklist)
            return httpx.Response(200, json=checklist)
        if len(parts) == 3 and parts[0] == "checklists" and parts[2] == "checkItems":
            self.calls.append(request)
            checklist = next(
                item
                for lists in self.checklists.values()
                for item in lists
                if item["id"] == parts[1]
            )
            self.items_created += 1
            item = {
                "id": f"check-{self.items_created}",
                "name": json.loads(request.content)["name"],
                "state": "incomplete",
            }
            checklist["checkItems"].append(item)
            return httpx.Response(200, json=item)
        if len(parts) == 4 and parts[0] == "cards" and parts[2] == "checkItem":
            self.calls.append(request)
            item = next(
                item
                for checklist in self.checklists[parts[1]]
                for item in checklist["checkItems"]
                if item["id"] == parts[3]
            )
            item["state"] = json.loads(request.content)["state"]
            return httpx.Response(200, json=item)
        return super().handle(request)

    def check(self, card_id, index, complete=True):
        self.checklists[card_id][0]["checkItems"][index]["state"] = (
            "complete" if complete else "incomplete"
        )

    @property
    def moves(self):
        return [
            call
            for call in self.calls
            if call.method == "PUT" and json_body(call).keys() == {"idList"}
        ]


def json_body(request):
    return (
        json.loads(request.content)
        if request.headers.get("content-type", "").startswith("application/json")
        else {}
    )


def fixture_data(*, missing=False):
    data = order_data()
    if not missing:
        data["items"][1]["dimensions"] = "2,70 x 0,70 m"
    return data


def setup(tmp_path, *, missing=False, handler=None):
    settings = replace(config(tmp_path), production_automation=True)
    board = ProductionBoard()
    transport = httpx.MockTransport(handler(board) if handler else board.handle)

    def factory(config):
        return TrelloClient(config, transport=transport)

    batch = BatchOrder.model_validate(fixture_data(missing=missing))
    client = factory(settings)
    try:
        submit_batch(batch, settings, client)
    finally:
        client.close()
    watcher = ProductionWatcher(lambda: settings, factory)
    return settings, board, batch, watcher, factory


def test_setup_is_automatic_and_idempotent_without_moving_cards(tmp_path):
    settings, board, batch, watcher, _ = setup(tmp_path)
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "setup"
    watcher.run_once()
    before = len([call for call in board.calls if call.method == "POST"])
    watcher.run_once()
    result = get_batch(batch.order_id, settings)
    assert all(item["production"]["status"] == "active" for item in result["cards"])
    assert len(board.checklists) == 2
    assert all(
        checklist[0]["name"] == CHECKLIST_NAME and len(checklist[0]["checkItems"]) == 3
        for checklist in board.checklists.values()
    )
    assert before == len([call for call in board.calls if call.method == "POST"])
    assert not board.moves
    assert len(board.posts) == 2


@pytest.mark.parametrize(
    "item_index,product,board_id", [(0, "dtf_uv", "digital-board"), (1, "windflag", "sub-board")]
)
def test_checklist_confirmations_move_same_card_through_its_exact_route(
    tmp_path, item_index, product, board_id
):
    settings, board, batch, watcher, factory = setup(tmp_path)
    watcher.run_once()
    card_id = f"created-{item_index + 1}"
    before = deepcopy(board.cards[card_id])
    for index in range(3):
        board.check(card_id, index)
        watcher.run_once()
        item = get_batch(batch.order_id, settings)["cards"][item_index]
        assert item["card"]["id"] == card_id
        assert item["production"]["step"] == index + 1
        assert item["verification"]["confirmed"]
        assert item["verification"]["list_name"] == ROUTES[product].lists[index + 1]
        assert board.cards[card_id]["idList"] == f"{board_id}-flow-{index + 1}"
    result = get_batch(batch.order_id, settings)
    assert result["cards"][item_index]["production"]["status"] == "finished"
    assert result["cards"][item_index]["revision"] == 4
    assert (
        len([event for event in result["activities"] if event["kind"] == "production_moved"]) == 3
    )
    assert {key: value for key, value in board.cards[card_id].items() if key != "idList"} == {
        key: value for key, value in before.items() if key != "idList"
    }
    with_client = factory(settings)
    try:
        assert sync_batch(batch.order_id, settings, with_client)["cards"][item_index][
            "verification"
        ]["confirmed"]
        assert submit_batch(batch, settings, with_client)["duplicate"]
    finally:
        with_client.close()
    assert len(board.posts) == 2
    moves = len(board.moves)
    watcher.run_once()
    assert len(board.moves) == moves


def test_elapsed_time_alone_never_advances_a_card(tmp_path):
    _, board, _, watcher, _ = setup(tmp_path)
    for _ in range(5):
        watcher.run_once()
    assert not board.moves


def test_missing_details_block_even_a_checked_production_confirmation(tmp_path):
    settings, board, batch, watcher, factory = setup(tmp_path, missing=True)
    watcher.run_once()
    board.check("created-2", 0)
    watcher.run_once()
    flow = get_batch(batch.order_id, settings)["cards"][1]["production"]
    assert flow["status"] == "blocked"
    assert board.cards["created-2"]["idList"] == "sub-board-draft"
    assert not board.moves
    client = factory(settings)
    try:
        complete_item(
            batch.order_id,
            "2",
            CompletionRequest(revision=1, changes={"dimensions": "2 x 1 m"}),
            settings,
            client,
        )
    finally:
        client.close()
    watcher.run_once()
    assert board.cards["created-2"]["idList"] == "sub-board-flow-1"


def test_out_of_order_checks_are_blocked_until_corrected(tmp_path):
    settings, board, batch, watcher, _ = setup(tmp_path)
    watcher.run_once()
    board.check("created-1", 1)
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "blocked"
    assert not board.moves
    board.check("created-1", 1, False)
    board.check("created-1", 0)
    watcher.run_once()
    assert len(board.moves) == 1


@pytest.mark.parametrize(
    "tamper", ["board", "column", "title", "description", "due", "closed", "complete", "template"]
)
def test_external_changes_pause_automation_without_writes(tmp_path, tamper):
    settings, board, batch, watcher, _ = setup(tmp_path)
    watcher.run_once()
    board.check("created-1", 0)
    card = board.cards["created-1"]
    mutations = {
        "board": ("idBoard", "another-production-board"),
        "column": ("idList", "manually-moved"),
        "title": ("name", "Staff title"),
        "description": ("desc", "Staff description"),
        "due": ("due", "2027-01-01T18:00:00-03:00"),
        "closed": ("closed", True),
        "complete": ("dueComplete", True),
        "template": ("isTemplate", True),
    }
    key, value = mutations[tamper]
    card[key] = value
    board.calls.clear()
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "paused"
    assert not any(call.method in {"PUT", "POST", "DELETE"} for call in board.calls)


def test_removed_confirmation_pauses_without_moving_backwards(tmp_path):
    settings, board, batch, watcher, _ = setup(tmp_path)
    watcher.run_once()
    board.check("created-1", 0)
    watcher.run_once()
    board.check("created-1", 0, False)
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "paused"
    assert board.cards["created-1"]["idList"] == "digital-board-flow-1"
    assert len(board.moves) == 1
    board.check("created-1", 0)
    watcher.action(batch.order_id, "1", "resume")
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "active"
    assert len(board.moves) == 1


def test_resume_requires_card_back_in_its_last_confirmed_stage(tmp_path):
    settings, board, batch, watcher, _ = setup(tmp_path)
    watcher.run_once()
    board.cards["created-1"]["idList"] = "digital-board-flow-2"
    watcher.run_once()
    with pytest.raises(OrderConflict):
        watcher.action(batch.order_id, "1", "resume")
    assert not board.moves
    board.cards["created-1"]["idList"] = "digital-board-entry"
    watcher.action(batch.order_id, "1", "resume")
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "active"


@pytest.mark.parametrize("failure", ["move", "checklist", "checkitem", "readback"])
def test_lost_responses_recover_after_restart_without_duplicate_writes(tmp_path, failure):
    failed = False
    lose_next_read = False

    def handler(board):
        def handle(request):
            nonlocal failed, lose_next_read
            if lose_next_read and request.method == "GET" and "/cards/" in request.url.path:
                lose_next_read = False
                return httpx.Response(503, json={"error": "unavailable"})
            response = board.handle(request)
            path = request.url.path
            is_move = request.method == "PUT" and json_body(request).keys() == {"idList"}
            matches = (
                (failure == "move" and is_move)
                or (
                    failure == "checklist"
                    and request.method == "POST"
                    and path.endswith("/checklists")
                )
                or (
                    failure == "checkitem"
                    and request.method == "POST"
                    and path.endswith("/checkItems")
                )
            )
            if failure == "readback" and is_move and not failed:
                failed = True
                lose_next_read = True
            elif matches and not failed:
                failed = True
                raise httpx.ReadTimeout("Applied, response lost", request=request)
            return response

        return handle

    settings, board, batch, watcher, factory = setup(tmp_path, handler=handler)
    watcher.run_once()
    if failure in {"checklist", "checkitem"}:
        watcher = ProductionWatcher(lambda: settings, factory)
        watcher.run_once()
    assert len(board.checklists) == 2
    assert board.items_created == 6
    board.check("created-1", 0)
    watcher.run_once()
    watcher = ProductionWatcher(lambda: settings, factory)
    watcher.run_once()
    result = get_batch(batch.order_id, settings)
    assert result["cards"][0]["production"]["step"] == 1
    assert result["cards"][0]["verification"]["confirmed"]
    assert len(board.moves) == 1
    assert (
        len([event for event in result["activities"] if event["kind"] == "production_moved"]) == 1
    )
    assert board.items_created == 6
    assert len(board.posts) == 2


def test_uncertain_setup_does_not_blindly_create_another_checklist(tmp_path):
    attempts = []

    def handler(board):
        def handle(request):
            if request.method == "POST" and request.url.path.endswith(
                "/cards/created-1/checklists"
            ):
                attempts.append(request)
                raise httpx.ReadTimeout("Unknown outcome", request=request)
            return board.handle(request)

        return handle

    settings, _, batch, watcher, _ = setup(tmp_path, handler=handler)
    for _ in range(3):
        watcher.run_once()
    assert len(attempts) == 1
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "setup"


def test_recovered_move_requires_its_confirmation_to_still_exist(tmp_path):
    def handler(board):
        def handle(request):
            response = board.handle(request)
            if request.method == "PUT" and json_body(request).keys() == {"idList"}:
                raise httpx.ReadTimeout("Applied, response lost", request=request)
            return response

        return handle

    settings, board, batch, watcher, factory = setup(tmp_path, handler=handler)
    watcher.run_once()
    board.check("created-1", 0)
    watcher.run_once()
    board.check("created-1", 0, False)
    ProductionWatcher(lambda: settings, factory).run_once()
    flow = get_batch(batch.order_id, settings)["cards"][0]["production"]
    assert flow["status"] == "paused"
    assert flow["step"] == 0
    assert board.cards["created-1"]["idList"] == "digital-board-flow-1"
    assert len(board.moves) == 1


@pytest.mark.parametrize("change", [{"board_key": "sublimacao"}, {"product_code": "windflag"}])
def test_saved_preview_cannot_redirect_a_production_route(tmp_path, change):
    settings, board, batch, watcher, _ = setup(tmp_path)
    db = open_database(settings.database)
    try:
        child_id = f"{batch.order_id}__1"
        result = json.loads(
            db.execute("SELECT result FROM orders WHERE order_id=?", (child_id,)).fetchone()[0]
        )
        result["preview"].update(change)
        db.execute("UPDATE orders SET result=? WHERE order_id=?", (json.dumps(result), child_id))
        db.commit()
    finally:
        db.close()
    board.calls.clear()
    watcher.run_once()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["status"] == "paused"
    assert not any(
        call.method in {"PUT", "POST", "DELETE"} and "created-1" in call.url.path
        for call in board.calls
    )


def test_api_only_confirms_the_next_step_and_watcher_performs_the_move(tmp_path, monkeypatch):
    settings, board, batch, watcher, _ = setup(tmp_path)
    watcher.run_once()
    monkeypatch.setattr(
        main, "get_settings", lambda: replace(settings, production_automation=False)
    )
    path = f"/orders/batch/{batch.order_id}/items/1/production"
    with TestClient(main.app) as client:
        client.app.state.production = watcher
        assert client.patch(path + "/checks/1", json={"checked": True}).status_code == 409
        assert client.patch(path + "/checks/0", json={"checked": False}).status_code == 422
        assert client.patch(path + "/checks/0", json={"checked": True}).status_code == 200
        assert not board.moves
        watcher.run_once()
        assert len(board.moves) == 1
        assert client.post(path + "/pause", json={}).status_code == 200
        assert (
            client.get(f"/orders/batch/{batch.order_id}").json()["cards"][0]["production"]["status"]
            == "paused"
        )
        assert client.post(path + "/resume", json={}).status_code == 200
        assert client.patch(path + "/checks/9", json={"checked": True}).status_code == 422


def test_background_worker_moves_without_a_browser_or_an_api_trigger(tmp_path):
    settings, board, batch, watcher, factory = setup(tmp_path)
    watcher.run_once()
    board.check("created-1", 0)
    moved = threading.Event()

    def client_factory(config):
        def handle(request):
            response = board.handle(request)
            if request.method == "PUT" and json_body(request).keys() == {"idList"}:
                moved.set()
            return response

        return TrelloClient(config, transport=httpx.MockTransport(handle))

    watcher = ProductionWatcher(lambda: settings, client_factory)
    watcher.start()
    try:
        assert moved.wait(timeout=3)
    finally:
        watcher.stop()
    assert get_batch(batch.order_id, settings)["cards"][0]["production"]["step"] == 1
    assert not watcher.thread.is_alive()
