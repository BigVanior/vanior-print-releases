"""Shared progress and cancellation primitives for long-running operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event


@dataclass(frozen=True)
class ProgressEvent:
    """One user-facing progress update."""

    stage: str
    message: str
    percent: int


ProgressCallback = Callable[[ProgressEvent], None]


class OperationCancelled(RuntimeError):
    """Raised when a caller requests cancellation of an active operation."""


def emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    message: str,
    percent: int,
) -> None:
    if callback is not None:
        callback(ProgressEvent(stage, message, max(0, min(100, percent))))


def check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("operation cancelled by user")
