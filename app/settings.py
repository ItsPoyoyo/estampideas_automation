import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    key: str = field(repr=False)
    token: str = field(repr=False)
    boards: dict[str, str]
    holidays: frozenset[date]
    database: Path
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    token_expires_at: float = 0
    template_ids: dict[str, str] = field(default_factory=dict)
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-5.4-mini"
    production_automation: bool = False
    blocked_board_ids: frozenset[str] = field(default_factory=frozenset, repr=False)

    @property
    def connected(self) -> bool:
        return bool((self.oauth_ready or (self.key and self.token)) and all(self.boards.values()))

    @property
    def oauth_ready(self) -> bool:
        return bool(self.access_token and self.token_expires_at > time.time())

    @property
    def can_authorize(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret)

    @property
    def ai_configured(self) -> bool:
        return bool(self.openai_api_key)


def get_settings() -> Settings:
    # Read each time, so saving .env is enough; credentials never go to the browser.
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    return Settings(
        key=values.get("TRELLO_API_KEY", "") or "",
        token=values.get("TRELLO_TOKEN", "") or "",
        boards={
            "digital": values.get("TRELLO_DIGITAL_BOARD_ID", "") or "",
            "sublimacao": values.get("TRELLO_SUBLIMACAO_BOARD_ID", "") or "",
        },
        holidays=frozenset(
            date.fromisoformat(x.strip())
            for x in (values.get("HOLIDAYS", "") or "").split(",")
            if x.strip()
        ),
        database=ROOT / "data" / "orders.sqlite3",
        oauth_client_id=values.get("TRELLO_OAUTH_CLIENT_ID", "") or "",
        oauth_client_secret=values.get("TRELLO_OAUTH_CLIENT_SECRET", "") or "",
        access_token=values.get("TRELLO_ACCESS_TOKEN", "") or "",
        token_expires_at=float(values.get("TRELLO_TOKEN_EXPIRES_AT", "0") or "0"),
        template_ids=json.loads(values.get("TRELLO_TEMPLATE_IDS", "{}") or "{}"),
        openai_api_key=values.get("OPENAI_API_KEY", "") or "",
        openai_model=values.get("OPENAI_MODEL", "gpt-5.4-mini") or "gpt-5.4-mini",
        production_automation=(values.get("PRODUCTION_AUTOMATION", "true") or "true").lower()
        == "true",
        blocked_board_ids=frozenset(
            board_id.strip()
            for board_id in (values.get("TRELLO_BLOCKED_BOARD_IDS", "") or "").split(",")
            if board_id.strip()
        ),
    )
