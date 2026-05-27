"""Typed error model. Every API error response carries a stable code so the
frontend can decide what to show and whether to auto-recover, instead of
substring-matching on a free-form `error` field."""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    CAMERA_AUTH = "CAMERA_AUTH"
    CAMERA_BUSY = "CAMERA_BUSY"
    CAMERA_NOT_CONFIGURED = "CAMERA_NOT_CONFIGURED"
    DOWNLOAD_EMPTY = "DOWNLOAD_EMPTY"
    DOWNLOAD_NO_VIDEO = "DOWNLOAD_NO_VIDEO"
    FFMPEG_FAILED = "FFMPEG_FAILED"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    BAD_REQUEST = "BAD_REQUEST"
    INTERNAL = "INTERNAL"


# Codes for which the frontend should kick off the auto-recover SSE stream.
RECOVERABLE = {Code.CAMERA_OFFLINE, Code.CAMERA_AUTH}


class ApiError(Exception):
    """Raised inside route handlers; converted to a structured JSON body
    by the global FastAPI exception handler."""

    __slots__ = ("code", "message", "status", "context")

    def __init__(
        self,
        code: Code,
        message: str,
        *,
        status: int = 500,
        context: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.context = context or {}

    def body(self) -> dict:
        return {
            "ok": False,
            "code": self.code.value,
            "error": self.message,
            "retryable": self.code in RECOVERABLE,
            **({"context": self.context} if self.context else {}),
        }
