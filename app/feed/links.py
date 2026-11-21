def message_link(chat_id: int, username: str | None, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    raw = str(chat_id)
    return f"https://t.me/c/{raw[4:]}/{message_id}" if raw.startswith("-100") else ""
