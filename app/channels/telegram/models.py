"""The slice of a Telegram Update that Ruko reads.

Models are deliberately lenient (`extra="ignore"`): Telegram adds fields all
the time and unknown ones must never break a webhook call.
"""

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class User(_Lenient):
    id: int
    is_bot: bool = False
    language_code: str | None = None
    # Profile fields. Attacker-controlled text: only ever shown after sanitising.
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


class Chat(_Lenient):
    id: int
    type: str


class PhotoSize(_Lenient):
    file_id: str
    width: int = 0
    height: int = 0
    file_size: int | None = None


class Document(_Lenient):
    file_id: str
    mime_type: str | None = None
    file_size: int | None = None


class MessageEntity(_Lenient):
    type: str
    offset: int = 0
    length: int = 0
    url: str | None = None  # set for "text_link": the real target hidden behind the text


class Message(_Lenient):
    message_id: int
    from_: User | None = Field(default=None, alias="from")
    chat: Chat
    date: int | None = None  # unix seconds, set by Telegram
    text: str | None = None
    caption: str | None = None
    entities: list[MessageEntity] = []
    caption_entities: list[MessageEntity] = []
    photo: list[PhotoSize] | None = None
    document: Document | None = None

    # Content we do not support. Only their presence matters, so a message
    # carrying one gets the "unsupported input" reply, while service messages
    # (which carry none of these) are ignored silently.
    voice: dict | None = None
    audio: dict | None = None
    video: dict | None = None
    video_note: dict | None = None
    animation: dict | None = None
    sticker: dict | None = None
    contact: dict | None = None
    location: dict | None = None
    venue: dict | None = None
    poll: dict | None = None
    dice: dict | None = None

    @property
    def has_unsupported_media(self) -> bool:
        return any(
            getattr(self, name) is not None
            for name in (
                "voice", "audio", "video", "video_note", "animation",
                "sticker", "contact", "location", "venue", "poll", "dice",
            )
        )  # fmt: skip


class CallbackQuery(_Lenient):
    """A press of an inline button. `data` is whatever the (possibly forged)
    client sent, so it is untrusted input."""

    id: str
    from_: User = Field(alias="from")
    message: Message | None = None
    data: str | None = None


class ChatMember(_Lenient):
    status: str
    user: User | None = None


class ChatMemberUpdated(_Lenient):
    """The bot's own status in a chat changed (e.g. a user blocked it)."""

    chat: Chat
    from_: User | None = Field(default=None, alias="from")
    old_chat_member: ChatMember | None = None
    new_chat_member: ChatMember


class Update(_Lenient):
    update_id: int
    message: Message | None = None
    callback_query: CallbackQuery | None = None
    my_chat_member: ChatMemberUpdated | None = None
