import email.utils
from collections.abc import AsyncGenerator
from datetime import datetime
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default
from typing import Any

import aioimaplib
import aiosmtplib

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails import EmailHandler
from mcp_email_server.emails.models import EmailData, EmailPageResponse
from mcp_email_server.log import logger


class EmailClient:
    def __init__(self, email_server: EmailServer, sender: str | None = None):
        self.email_server = email_server
        self.sender = sender or email_server.user_name

        self.imap_class = aioimaplib.IMAP4_SSL if self.email_server.use_ssl else aioimaplib.IMAP4

        self.smtp_use_tls = self.email_server.use_ssl
        self.smtp_start_tls = self.email_server.start_ssl

    def _parse_email_data(self, raw_email: bytes) -> dict[str, Any]:  # noqa: C901
        """Parse raw email data into a structured dictionary."""
        parser = BytesParser(policy=default)
        email_message = parser.parsebytes(raw_email)

        # Extract email parts
        subject = email_message.get("Subject", "")
        sender = email_message.get("From", "")
        date_str = email_message.get("Date", "")

        # Parse date
        try:
            date_tuple = email.utils.parsedate_tz(date_str)
            date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)) if date_tuple else datetime.now()
        except Exception:
            date = datetime.now()

        # Get body content
        body = ""
        attachments = []

        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Handle attachments
                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        attachments.append(filename)
                # Handle text parts
                elif content_type == "text/plain":
                    body_part = part.get_payload(decode=True)
                    if body_part:
                        charset = part.get_content_charset("utf-8")
                        try:
                            body += body_part.decode(charset)
                        except UnicodeDecodeError:
                            body += body_part.decode("utf-8", errors="replace")
        else:
            # Handle plain text emails
            payload = email_message.get_payload(decode=True)
            if payload:
                charset = email_message.get_content_charset("utf-8")
                try:
                    body = payload.decode(charset)
                except UnicodeDecodeError:
                    body = payload.decode("utf-8", errors="replace")

        return {
            "subject": subject,
            "from": sender,
            "body": body,
            "date": date,
            "attachments": attachments,
        }

    async def get_emails_stream(  # noqa: C901
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        is_unread: bool | None = None,
        is_flagged: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password)
            try:
                await imap.id(name="mcp-email-server", version="1.0.0")
            except Exception as e:
                logger.warning(f"IMAP ID command failed: {e!s}")
            await imap.select("INBOX")

            search_criteria = self._build_search_criteria(
                before, since, subject, body, text, from_address, to_address, is_unread, is_flagged
            )
            logger.info(f"Get: Search criteria: {search_criteria}")

            # Search for messages - use UID SEARCH for better compatibility
            _, messages = await imap.uid_search(*search_criteria)

            # Handle empty or None responses
            if not messages or not messages[0]:
                logger.warning("No messages returned from search")
                message_ids = []
            else:
                message_ids = messages[0].split()
                logger.info(f"Found {len(message_ids)} message IDs")
            start = (page - 1) * page_size
            end = start + page_size

            if order == "desc":
                message_ids.reverse()

            # Fetch each message
            for _, message_id in enumerate(message_ids[start:end]):
                try:
                    # Convert message_id from bytes to string
                    message_id_str = message_id.decode("utf-8")

                    # Fetch the email using UID - try different formats for compatibility
                    data = None
                    # Include FLAGS in fetch to get read/unread status
                    # IMPORTANT: Use BODY.PEEK first to avoid marking messages as read
                    fetch_formats = ["(BODY.PEEK[] FLAGS)", "BODY.PEEK[] FLAGS", "(BODY[] FLAGS)", "(RFC822 FLAGS)"]

                    for fetch_format in fetch_formats:
                        try:
                            _, data = await imap.uid("fetch", message_id_str, fetch_format)

                            if data and len(data) > 0:
                                # Check if we got actual email content or just metadata
                                has_content = False
                                for item in data:
                                    if (
                                        isinstance(item, bytes)
                                        and b"FETCH (" in item
                                        and b"RFC822" not in item
                                        and b"BODY" not in item
                                    ):
                                        # This is just metadata (like 'FETCH (UID 71998)'), not actual content
                                        continue
                                    elif isinstance(item, bytes | bytearray) and len(item) > 100:
                                        # This looks like email content
                                        has_content = True
                                        break

                                if has_content:
                                    break
                                else:
                                    data = None  # Try next format

                        except Exception as e:
                            logger.debug(f"Fetch format {fetch_format} failed: {e}")
                            data = None

                    if not data:
                        logger.error(f"Failed to fetch UID {message_id_str} with any format")
                        continue

                    # Find the email data and flags in the response
                    raw_email = None
                    email_flags = []

                    # Debug log the raw IMAP response structure
                    logger.debug(f"IMAP response for UID {message_id_str}: {len(data)} items")
                    for i, item in enumerate(data):
                        if isinstance(item, bytes):
                            logger.debug(f"  Item {i} (bytes): {item[:200]}...")  # First 200 bytes

                    # Parse the IMAP response to extract both email content and flags
                    for item in data:
                        if isinstance(item, bytes) and b"FLAGS (" in item:
                            # Extract flags from response like: b'1 FETCH (FLAGS (\\Seen \\Answered) RFC822 {size}'
                            start = item.find(b"FLAGS (") + 7
                            end = item.find(b")", start)
                            if start > 6 and end > start:
                                flags_str = item[start:end].decode("utf-8", errors="ignore")
                                email_flags = flags_str.split()
                                logger.debug(f"Parsed flags from IMAP: {email_flags}")

                        if isinstance(item, bytes | bytearray) and len(item) > 100:
                            # Skip IMAP protocol responses
                            if (
                                isinstance(item, bytes)
                                and b"FETCH" in item
                                and not (b"From:" in item or b"Subject:" in item)
                            ):
                                continue
                            # This is likely the email content
                            raw_email = bytes(item) if isinstance(item, bytearray) else item

                    if raw_email:
                        try:
                            parsed_email = self._parse_email_data(raw_email)
                            # Add flag information to the parsed email
                            parsed_email["flags"] = email_flags
                            parsed_email["is_read"] = r"\Seen" in email_flags
                            parsed_email["is_answered"] = r"\Answered" in email_flags
                            parsed_email["is_flagged"] = r"\Flagged" in email_flags
                            parsed_email["is_deleted"] = r"\Deleted" in email_flags
                            parsed_email["is_draft"] = r"\Draft" in email_flags
                            parsed_email["is_recent"] = r"\Recent" in email_flags
                            yield parsed_email
                        except Exception as e:
                            # Log error but continue with other emails
                            logger.error(f"Error parsing email: {e!s}")
                    else:
                        logger.error(f"Could not find email data in response for message ID: {message_id_str}")
                except Exception as e:
                    logger.error(f"Error fetching message {message_id}: {e!s}")
        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    @staticmethod
    def _add_flag_criteria(search_criteria: list, is_unread: bool | None, is_flagged: bool | None) -> None:
        """Add flag-based search criteria to the list."""
        if is_unread is True:
            search_criteria.append("UNSEEN")
        elif is_unread is False:
            search_criteria.append("SEEN")

        if is_flagged is True:
            search_criteria.append("FLAGGED")
        elif is_flagged is False:
            search_criteria.append("UNFLAGGED")

    @staticmethod
    def _build_search_criteria(
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        is_unread: bool | None = None,
        is_flagged: bool | None = None,
    ):
        search_criteria = []
        if before:
            search_criteria.extend(["BEFORE", before.strftime("%d-%b-%Y").upper()])
        if since:
            search_criteria.extend(["SINCE", since.strftime("%d-%b-%Y").upper()])
        if subject:
            search_criteria.extend(["SUBJECT", subject])
        if body:
            search_criteria.extend(["BODY", body])
        if text:
            search_criteria.extend(["TEXT", text])
        if from_address:
            search_criteria.extend(["FROM", from_address])
        if to_address:
            search_criteria.extend(["TO", to_address])

        # Add flag-based search criteria
        EmailClient._add_flag_criteria(search_criteria, is_unread, is_flagged)

        # If no specific criteria, search for ALL
        if not search_criteria:
            search_criteria = ["ALL"]

        return search_criteria

    async def get_email_count(
        self,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        is_unread: bool | None = None,
        is_flagged: bool | None = None,
    ) -> int:
        imap = self.imap_class(self.email_server.host, self.email_server.port)
        try:
            # Wait for the connection to be established
            await imap._client_task
            await imap.wait_hello_from_server()

            # Login and select inbox
            await imap.login(self.email_server.user_name, self.email_server.password)
            await imap.select("INBOX")
            search_criteria = self._build_search_criteria(
                before, since, subject, body, text, from_address, to_address, is_unread, is_flagged
            )
            logger.info(f"Count: Search criteria: {search_criteria}")
            # Search for messages and count them - use UID SEARCH for consistency
            _, messages = await imap.uid_search(*search_criteria)
            return len(messages[0].split())
        finally:
            # Ensure we logout properly
            try:
                await imap.logout()
            except Exception as e:
                logger.info(f"Error during logout: {e}")

    async def send_email(
        self, recipients: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None
    ):
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)

        # Add CC header if provided (visible to recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)

        # Note: BCC recipients are not added to headers (they remain hidden)
        # but will be included in the actual recipients for SMTP delivery

        async with aiosmtplib.SMTP(
            hostname=self.email_server.host,
            port=self.email_server.port,
            start_tls=self.smtp_start_tls,
            use_tls=self.smtp_use_tls,
        ) as smtp:
            await smtp.login(self.email_server.user_name, self.email_server.password)

            # Create a combined list of all recipients for delivery
            all_recipients = recipients.copy()
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            await smtp.send_message(msg, recipients=all_recipients)


class ClassicEmailHandler(EmailHandler):
    def __init__(self, email_settings: EmailSettings):
        self.email_settings = email_settings
        self.incoming_client = EmailClient(email_settings.incoming)
        self.outgoing_client = EmailClient(
            email_settings.outgoing,
            sender=f"{email_settings.full_name} <{email_settings.email_address}>",
        )

    async def get_emails(
        self,
        page: int = 1,
        page_size: int = 10,
        before: datetime | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        body: str | None = None,
        text: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        is_unread: bool | None = None,
        is_flagged: bool | None = None,
    ) -> EmailPageResponse:
        emails = []
        async for email_data in self.incoming_client.get_emails_stream(
            page, page_size, before, since, subject, body, text, from_address, to_address, order, is_unread, is_flagged
        ):
            emails.append(EmailData.from_email(email_data))
        total = await self.incoming_client.get_email_count(
            before, since, subject, body, text, from_address, to_address, is_unread, is_flagged
        )
        return EmailPageResponse(
            page=page,
            page_size=page_size,
            before=before,
            since=since,
            subject=subject,
            body=body,
            text=text,
            emails=emails,
            total=total,
        )

    async def send_email(
        self, recipients: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None
    ) -> None:
        await self.outgoing_client.send_email(recipients, subject, body, cc, bcc)
