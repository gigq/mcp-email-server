from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EmailData(BaseModel):
    subject: str
    sender: str
    body: str
    date: datetime
    attachments: list[str]
    # IMAP flags
    is_read: bool = False  # \Seen flag
    is_answered: bool = False  # \Answered flag
    is_flagged: bool = False  # \Flagged flag
    is_deleted: bool = False  # \Deleted flag
    is_draft: bool = False  # \Draft flag
    is_recent: bool = False  # \Recent flag
    flags: list[str] = []  # Raw flags from IMAP

    @classmethod
    def from_email(cls, email: dict[str, Any]):
        return cls(
            subject=email["subject"],
            sender=email["from"],
            body=email["body"],
            date=email["date"],
            attachments=email["attachments"],
            is_read=email.get("is_read", False),
            is_answered=email.get("is_answered", False),
            is_flagged=email.get("is_flagged", False),
            is_deleted=email.get("is_deleted", False),
            is_draft=email.get("is_draft", False),
            is_recent=email.get("is_recent", False),
            flags=email.get("flags", []),
        )


class EmailPageResponse(BaseModel):
    page: int
    page_size: int
    before: datetime | None
    since: datetime | None
    subject: str | None
    body: str | None
    text: str | None
    emails: list[EmailData]
    total: int
