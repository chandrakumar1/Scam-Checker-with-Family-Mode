"""Helpers for the end-to-end Family Mode tests through TelegramService.

Everything runs against the in-memory FakeTelegram (httpx.MockTransport) and a
real FamilyService on a temporary SQLite file. No network, no LLM.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.channels.telegram.callback_tokens import CallbackTokenCodec
from app.channels.telegram.emergency import EmergencyHandler
from app.channels.telegram.family_alerts import FamilyAlertSender
from app.channels.telegram.family_handler import FamilyTelegramHandler
from app.channels.telegram.service import TelegramService
from app.db.engine import init_db, make_engine
from app.family.policy import DEFAULT_POLICY, FamilyPolicy
from app.family.service import AsyncFamilyService, FamilyService
from tests.family_helpers import Clock, all_alerts, all_links, all_people, link_row
from tests.telegram_fakes import TOKEN, FakeAnalyzer, FakeTelegram, api_error, parse

BOT_USERNAME = "ruko_dev_bot"
INVITE_RE = re.compile(r"https://t\.me/(\w+)\?start=(fam_[A-Za-z0-9_-]{22})")


@dataclass(frozen=True)
class User:
    id: int
    first_name: str
    username: str | None = None
    language_code: str | None = "en"
    last_name: str | None = None
    is_bot: bool = False

    def raw(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "is_bot": self.is_bot, "first_name": self.first_name}
        for key in ("username", "language_code", "last_name"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        return data


# Distinctive names, so the privacy tests can search the logs for them.
ZEBRINA = User(1001, "Zebrina", "zebrina_p", "en")
PRATT = User(1005, "Zebrina", "z_pratt", "en", "Pratt")  # has a last name: the display name is "Zebrina Pratt"
QUENTIN = User(2001, "Quentin", "quentin_r", "hi")
ANOUK = User(2002, "Anouk", "anouk_m", "en")
MEERA = User(2003, "Meera", None, "en")


def unix(moment: datetime) -> int:
    return int(moment.timestamp())


def result_for(
    text: str = "Hello there",
    verdict: str | None = "likely_scam",
    *,
    label: str = "other",
    explanation: str = "Short explanation.",
):
    """An AnalysisResult with a chosen verdict (None = the LLM failed), LLM label and explanation."""
    from app.analyzer.combine import combine
    from app.analyzer.llm import LLMAnalysis
    from app.analyzer.rules import check_rules

    llm = None
    if verdict is not None:
        llm = LLMAnalysis(
            verdict=verdict, scam_type=label, red_flags=["flag"], explanation=explanation, what_to_do=["Do step one."]
        )
    return combine(check_rules(text), llm)


def message_update(user: User, text: str, *, update_id: int, date: int, message_id: int = 10,
                   chat_type: str = "private", chat_id: int | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": user.raw(),
            "chat": {"id": user.id if chat_id is None else chat_id, "type": chat_type},
            "date": date,
            "text": text,
        },
    }


def callback_update(user: User, data: str | None, *, update_id: int, message_date: int, message_id: int = 500,
                    chat_id: int | None = None, chat_type: str = "private", with_message: bool = True,
                    query_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    query: dict[str, Any] = {"id": query_id or f"cbq-{update_id}", "from": user.raw(), "chat_instance": "ci"}
    if data is not None:
        query["data"] = data
    if with_message:
        query["message"] = {
            "message_id": message_id,
            "date": message_date,
            "chat": {"id": user.id if chat_id is None else chat_id, "type": chat_type},
        }
    return {"update_id": update_id, "callback_query": query}


def member_update(user: User, status: str, *, update_id: int, chat_type: str = "private") -> dict[str, Any]:
    bot = {"id": 99, "is_bot": True, "first_name": "Ruko"}
    return {
        "update_id": update_id,
        "my_chat_member": {
            "chat": {"id": user.id, "type": chat_type},
            "from": user.raw(),
            "date": 1_700_000_000,
            "old_chat_member": {"user": bot, "status": "member"},
            "new_chat_member": {"user": bot, "status": status},
        },
    }


class FamilyFakeTelegram(FakeTelegram):
    """FakeTelegram that can refuse messages to chosen chats, like a user who blocked the bot."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked_chats: set[int] = set()
        self.delivered: list[dict[str, Any]] = []  # sendMessage payloads that were accepted
        self.refused: list[dict[str, Any]] = []
        # Called with the payload just before a sendMessage is processed (lets a test inspect the database mid-flow).
        self.on_send: Any = None
        self.send_log: list[str] = []  # "<chat_id>:<first 12 chars of text>" for every attempt, in order
        self.error_chats: dict[int, int] = {}  # chat id -> HTTP status that every send to it fails with

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            self.send_log.append(f"{payload.get('chat_id')}:{str(payload.get('text'))[:12]}")
            if self.on_send is not None:
                self.on_send(payload)
            if payload.get("chat_id") in self.blocked_chats:
                self.requests.append(request)
                self.calls.append(("sendMessage", payload))
                self.refused.append(payload)
                return api_error(403, "Forbidden: bot was blocked by the user")
            if payload.get("chat_id") in self.error_chats:
                self.requests.append(request)
                self.calls.append(("sendMessage", payload))
                self.refused.append(payload)
                return api_error(self.error_chats[payload["chat_id"]], "Internal Server Error")
            response = super()._handler(request)
            if response.status_code == 200:
                self.delivered.append(payload)
            return response
        return super()._handler(request)


class Harness:
    """The real TelegramService + FamilyTelegramHandler + FamilyService, with a fake Telegram."""

    def __init__(self, tmp_path: Path, *, policy: FamilyPolicy = DEFAULT_POLICY, bot_username: str | None = BOT_USERNAME,
                 with_family: bool = True, with_emergency: bool = True,
                 max_concurrent: int = 5) -> None:  # fmt: skip
        self.tg = FamilyFakeTelegram()
        self.clock = Clock()
        self.engine = make_engine(f"sqlite:///{tmp_path / 'family.db'}")
        init_db(self.engine)
        self.family = FamilyService(self.engine, enabled=True, policy=policy, clock=self.clock)
        self.analyzer = FakeAnalyzer()
        self.client = self.tg.client()
        async_family = AsyncFamilyService(self.family)
        self.handler = (
            FamilyTelegramHandler(
                self.client,
                async_family,
                bot_username=bot_username,
                invite_ttl_hours=policy.invite_ttl.total_seconds() / 3600,
                max_helpers=policy.max_helpers,
                max_pending_invites=policy.max_pending_invites,
                clock=self.clock,
            )
            if with_family
            else None
        )
        # Alerts exist only with Family Mode; the emergency handler exists either way (it is always on).
        self.alert_sender = FamilyAlertSender(self.client, async_family) if with_family else None
        self.codec = CallbackTokenCodec(TOKEN, ttl=timedelta(hours=24))
        self.emergency = (
            EmergencyHandler(self.client, self.codec, self.alert_sender, clock=self.clock) if with_emergency else None
        )
        self.service = TelegramService(
            self.client, analyze=self.analyzer, family=self.handler, alerts=self.alert_sender, emergency=self.emergency,
            max_concurrent=max_concurrent,
        )  # fmt: skip
        self._update_id = 0

    # -- sending updates ------------------------------------------------------------------------------------

    def _next_id(self) -> int:
        self._update_id += 1
        return self._update_id

    def now_unix(self) -> int:
        return unix(self.clock.now)

    async def feed(self, payload: dict[str, Any]) -> None:
        await self.service.handle_update(parse(payload))

    async def say(self, user: User, text: str, **kwargs: Any) -> None:
        await self.feed(message_update(user, text, update_id=self._next_id(), date=self.now_unix(), **kwargs))

    async def tap(self, user: User, data: str | None, *, age: timedelta = timedelta(0), **kwargs: Any) -> None:
        await self.feed(
            callback_update(user, data, update_id=self._next_id(), message_date=unix(self.clock.now - age), **kwargs)
        )

    async def member(self, user: User, status: str, **kwargs: Any) -> None:
        await self.feed(member_update(user, status, update_id=self._next_id(), **kwargs))

    # -- reading what was sent ---------------------------------------------------------------------------------

    def sent_to(self, user: User | int) -> list[dict[str, Any]]:
        chat = user if isinstance(user, int) else user.id
        return [p for p in self.tg.delivered if p["chat_id"] == chat]

    def last_to(self, user: User | int) -> dict[str, Any]:
        return self.sent_to(user)[-1]

    def texts_to(self, user: User | int) -> list[str]:
        return [p["text"] for p in self.sent_to(user)]

    @staticmethod
    def buttons(payload: dict[str, Any]) -> list[dict[str, str]]:
        markup = payload.get("reply_markup") or {}
        return [button for row in markup.get("inline_keyboard", []) for button in row]

    def data_of(self, user: User | int, action: str, *, index: int = 0) -> str:
        """The callback data of the `index`-th button for `action` on the latest message that has one."""
        for payload in reversed(self.sent_to(user)):
            matching = [b["callback_data"] for b in self.buttons(payload) if b["callback_data"].startswith(f"fam:{action}")]
            if matching:
                return matching[index]
        raise AssertionError(f"no {action!r} button was sent to {user}")

    def em_data(self, user: User | int, action: str, *, index: int = 0) -> str:
        """Callback data of the latest emergency button (`em:<action>:<token>`) sent to `user`."""
        for payload in reversed(self.sent_to(user)):
            matching = [b["callback_data"] for b in self.buttons(payload) if b["callback_data"].startswith(f"em:{action}:")]
            if matching:
                return matching[index]
        raise AssertionError(f"no emergency {action!r} button was sent to {user}")

    def has_emergency_button(self, payload: dict[str, Any]) -> bool:
        return any(b["callback_data"].startswith("em:") for b in self.buttons(payload))

    def invite_link(self, user: User) -> tuple[str, str]:
        """(full link, start payload) from the latest invitation message sent to `user`."""
        for text in reversed(self.texts_to(user)):
            match = INVITE_RE.search(text)
            if match:
                return match.group(0), match.group(2)
        raise AssertionError("no invitation link was sent")

    def answers(self) -> list[dict[str, Any]]:
        return self.tg.method_calls("answerCallbackQuery")

    def removed_buttons(self) -> list[dict[str, Any]]:
        return self.tg.method_calls("editMessageReplyMarkup")

    # -- the database, for asserting state ------------------------------------------------------------------------

    def statuses(self) -> list[str]:
        return [link.status for link in all_links(self.engine)]

    def only_link(self):
        [link] = all_links(self.engine)
        return link

    def people(self):
        return all_people(self.engine)

    def alerts(self):
        return all_alerts(self.engine)

    def link(self, ref: str):
        return link_row(self.engine, ref)

    # -- scripted flows ------------------------------------------------------------------------------------------------

    async def invite(self, protected: User) -> str:
        """Run /family -> add -> yes for `protected`; returns the start payload from the link."""
        await self.say(protected, "/family")
        await self.tap(protected, "fam:add")
        await self.tap(protected, self.data_of(protected, "addy"))
        return self.invite_link(protected)[1]

    async def link_up(self, protected: User, guardian: User) -> str:
        """Run the whole consent flow through Telegram; returns the ref of the link it created."""
        already_active = {link.ref for link in all_links(self.engine) if link.status == "active"}
        payload = await self.invite(protected)
        await self.say(guardian, f"/start {payload}")
        await self.tap(guardian, self.data_of(guardian, "acc"))
        await self.tap(protected, self.data_of(protected, "yes"))
        [ref] = {link.ref for link in all_links(self.engine) if link.status == "active"} - already_active
        return ref

    def close(self) -> None:
        self.engine.dispose()


__all__ = [
    "ANOUK", "BOT_USERNAME", "FamilyFakeTelegram", "Harness", "MEERA", "PRATT", "QUENTIN", "TOKEN", "User", "ZEBRINA",
    "callback_update", "member_update", "message_update", "unix",
]  # fmt: skip
