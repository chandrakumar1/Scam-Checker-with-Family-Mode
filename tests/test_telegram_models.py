from app.channels.telegram.models import Update
from tests.telegram_fakes import document_update, other_update, parse, photo_update, text_update


def test_text_update():
    update = parse(text_update("hello", update_id=7, language_code="hi", message_id=42))
    assert update.update_id == 7
    message = update.message
    assert message.text == "hello"
    assert message.message_id == 42
    assert message.from_.language_code == "hi"
    assert message.from_.is_bot is False
    assert message.chat.type == "private"


def test_photo_update_with_caption():
    message = parse(photo_update("p1", caption="look", sizes=[(90, 90, 10), (800, 600, 5000)])).message
    assert message.caption == "look"
    assert [p.file_size for p in message.photo] == [10, 5000]
    assert message.photo[-1].file_id == "p1"


def test_document_update():
    document = parse(document_update("d1", mime_type="image/png", file_size=77)).message.document
    assert (document.file_id, document.mime_type, document.file_size) == ("d1", "image/png", 77)


def test_hidden_link_entities():
    entities = [{"type": "text_link", "offset": 0, "length": 5, "url": "https://bit.ly/x"}]
    message = parse(text_update("Claim", entities=entities)).message
    assert message.entities[0].type == "text_link"
    assert message.entities[0].url == "https://bit.ly/x"


def test_unknown_fields_are_ignored():
    payload = text_update("hi")
    payload["brand_new_field"] = {"a": 1}
    payload["message"]["another_new_field"] = [1, 2, 3]
    assert parse(payload).message.text == "hi"


def test_update_without_message():
    update = Update.model_validate({"update_id": 5, "edited_message": {"message_id": 1}})
    assert update.message is None


def test_unsupported_media_detection():
    assert parse(other_update(voice={"file_id": "v"})).message.has_unsupported_media
    assert parse(other_update(sticker={"file_id": "s"})).message.has_unsupported_media
    assert not parse(text_update("hi")).message.has_unsupported_media
    # A service message carries none of the supported or unsupported content.
    assert not parse(other_update(new_chat_title="x")).message.has_unsupported_media
