from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

import certifi
import httpx

from maxapi.types import InlineKeyboardMarkup, User, message_from_dict

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api2.max.ru"


_RUSSIAN_ROOT_CA = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(_RUSSIAN_ROOT_CA))
    return ctx


class MaxApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"MAX API error {status_code} [{code}]: {message}")


class MaxForbiddenError(MaxApiError):
    """Raised on HTTP 403 — bot blocked by user, or no access to the chat."""


class MaxClient:
    def __init__(self, token: str, base_url: str = BASE_URL):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": token},
            timeout=35.0,
            verify=_build_ssl_context(),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                        json: dict | None = None, timeout: float | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._client.request(method, path, params=params, json=json, timeout=timeout)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {}
            message = err.get("message") or err.get("error") or resp.text
            code = err.get("code", "")
            error_cls = MaxForbiddenError if resp.status_code == 403 else MaxApiError
            raise error_cls(resp.status_code, code, message)
        if resp.content:
            return resp.json()
        return {}

    # ------------------------------------------------------------------
    async def get_me(self) -> User:
        data = await self._request("GET", "/me")
        return User(id=data["user_id"], name=data.get("name"), username=data.get("username"))

    # ------------------------------------------------------------------
    async def send_message(self, text: str, *, user_id: int | None = None, chat_id: int | None = None,
                            keyboard: InlineKeyboardMarkup | None = None, format: str = "html"):
        body: dict = {"text": text, "format": format}
        if keyboard is not None:
            body["attachments"] = [keyboard.to_max_attachment()]
        data = await self._request(
            "POST", "/messages", params={"user_id": user_id, "chat_id": chat_id}, json=body
        )
        return message_from_dict(data["message"], self)

    async def edit_message(self, message_id: str, text: str | None, *,
                            keyboard: InlineKeyboardMarkup | None = None, format: str = "html") -> None:
        body: dict = {"format": format}
        if text is not None:
            body["text"] = text
        if keyboard is not None:
            body["attachments"] = [keyboard.to_max_attachment()]
        await self._request("PUT", "/messages", params={"message_id": message_id}, json=body)

    async def delete_message(self, message_id: str) -> None:
        await self._request("DELETE", "/messages", params={"message_id": message_id})

    async def answer_callback(self, callback_id: str, *, notification: str | None = None) -> None:
     
        body = {"notification": notification if notification is not None else ""}
        await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    # ------------------------------------------------------------------
    # Files (Excel reports): three-step upload — get an upload URL, POST
    # the binary to it, then attach the returned token to a message.
    # ------------------------------------------------------------------
    async def _upload_file_token(self, file_path: str, filename: str) -> str:
        endpoint = await self._request("POST", "/uploads", params={"type": "file"})
        upload_url = endpoint["url"]
        with open(file_path, "rb") as fh:
            upload_resp = await self._client.post(
                upload_url, files={"data": (filename, fh)}, timeout=120.0
            )
        upload_resp.raise_for_status()
        payload = upload_resp.json()
        token = payload.get("token")
        if not token:
            # Some upload responses nest the token under a per-type key
            # (matches the pattern shown for video/audio in the docs).
            for value in payload.values():
                if isinstance(value, dict) and "token" in value:
                    token = value["token"]
                    break
        if not token:
            raise MaxApiError(200, "upload.bad_response", f"Unexpected upload response shape: {payload}")
        return token

    async def _send_file_attachment(self, token: str, *, caption: str | None = None,
                                     user_id: int | None = None, chat_id: int | None = None,
                                     format: str = "html", attachment_type: str = "file"):
        body: dict = {"attachments": [{"type": attachment_type, "payload": {"token": token}}]}
        if caption:
            body["text"] = caption
            body["format"] = format

        delays = [1, 2, 3, 5, 8]
        for attempt, delay in enumerate([0, *delays]):
            if delay:
                await asyncio.sleep(delay)
            try:
                data = await self._request(
                    "POST", "/messages", params={"user_id": user_id, "chat_id": chat_id}, json=body
                )
                return message_from_dict(data["message"], self)
            except MaxApiError as exc:
                if exc.code != "attachment.not.ready" or attempt == len(delays):
                    raise
                logger.info("Attachment not processed yet, retrying in %ss (attempt %d/%d)",
                            delay or delays[0], attempt + 1, len(delays) + 1)

    async def send_document(self, file_path: str, filename: str, *, caption: str | None = None,
                             user_id: int | None = None, chat_id: int | None = None, format: str = "html"):
        token = await self._upload_file_token(file_path, filename)
        return await self._send_file_attachment(
            token, caption=caption, user_id=user_id, chat_id=chat_id, format=format
        )

    async def send_file_by_token(self, token: str, *, caption: str | None = None,
                                  user_id: int | None = None, chat_id: int | None = None,
                                  format: str = "html", attachment_type: str = "file"):
        """Re-sends an attachment MAX already has (e.g. a homework upload
        some student sent to the bot earlier) by its token, without
        downloading and re-uploading the binary. `attachment_type` should
        match whatever type MAX originally tagged the upload with (file,
        image, video, audio, ...) — sending it back as a plain "file" would
        lose the native photo/video rendering."""
        return await self._send_file_attachment(
            token, caption=caption, user_id=user_id, chat_id=chat_id, format=format,
            attachment_type=attachment_type,
        )

    # ------------------------------------------------------------------
    async def get_updates(self, *, marker: int | None = None, timeout: int = 30,
                           limit: int = 100) -> tuple[list[dict], int | None]:
        data = await self._request(
            "GET", "/updates",
            params={"marker": marker, "timeout": timeout, "limit": limit},
            timeout=timeout + 10,
        )
        return data.get("updates", []), data.get("marker")
