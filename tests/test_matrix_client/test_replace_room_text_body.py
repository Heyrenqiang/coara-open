"""Room text rebuild must use nio's from_dict contract, not init=False fields."""

from __future__ import annotations

from nio import RoomMessageText

from src.matrix_client.ingress_helpers import replace_room_text_body

_MARKER = "[COARA_QUOTE_MXC] mxc://h/abc [/COARA_QUOTE_MXC]"


def test_replace_room_text_body_keeps_identity_and_strips_formatted_marker() -> None:
    source = {
        "event_id": "e1",
        "sender": "@u:h",
        "origin_server_ts": 42,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": f"看这张 {_MARKER}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>看这张 {_MARKER}</p>",
        },
    }
    event = RoomMessageText.from_dict(source)
    event.decrypted = True
    event.verified = True

    rebuilt = replace_room_text_body(event, "看这张")

    assert isinstance(rebuilt, RoomMessageText)
    assert rebuilt.body == "看这张"
    assert rebuilt.sender == "@u:h"
    assert rebuilt.server_timestamp == 42
    assert rebuilt.decrypted is True
    assert rebuilt.verified is True
    assert "COARA_QUOTE_MXC" not in (rebuilt.formatted_body or "")
    assert event.body.endswith(_MARKER)
