"""Process-wide, per-book scheduling shared by outline and prose workers.

Reserve before starting a worker so submission order is deterministic. The worker
waits before reading story context; unrelated books never block each other.
"""
from __future__ import annotations

import os
import inspect
import hashlib
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from functools import wraps
from core.cli_scheduler import FileLock, check_cancelled


class StoryTaskStopped(Exception):
    """A queued task was stopped before it could touch story content."""


class StoryTaskBusy(ValueError):
    """An interactive mutation conflicts with a running or queued story task."""


@dataclass(frozen=True)
class StoryTaskTicket:
    book: str
    task_id: str


_condition = threading.Condition()
_queues: dict[str, deque[StoryTaskTicket]] = {}
_mutations: set[str] = set()
_local = threading.local()


def _book_key(path):
    if hasattr(path, "file_system"):
        path = path.file_system
    else:
        path = Path(path)
        if path.name != "file_system":
            path = path / "file_system"
    return os.path.normcase(str(Path(path).resolve()))


def reserve_story_task(book_path, task_id=None):
    ticket = StoryTaskTicket(_book_key(book_path), task_id or uuid.uuid4().hex)
    with _condition:
        if ticket.book in _mutations:
            raise StoryTaskBusy("本书正在修改或删除，请稍后重新提交写作任务。")
        _queues.setdefault(ticket.book, deque()).append(ticket)
        _condition.notify_all()
    return ticket


def story_lock_path(ws_or_path):
    """Keep the OS lock outside the book so Windows can delete the book safely."""
    key = _book_key(ws_or_path)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(key).parent.parent / ".writing-locks" / f"{digest}.lock"


def release_story_task(ticket):
    with _condition:
        queue = _queues.get(ticket.book)
        if queue and ticket in queue:
            queue.remove(ticket)
            if not queue:
                _queues.pop(ticket.book, None)
        _condition.notify_all()


def wait_for_story_task(ticket, *, pause_event, stop_event, cancel_event=None, on_wait=None):
    last_phase = None
    while True:
        if stop_event.is_set():
            raise StoryTaskStopped()
        paused = not pause_event.is_set()
        if not paused:
            check_cancelled(cancel_event)
        with _condition:
            queue = _queues.get(ticket.book)
            if not queue or ticket not in queue:
                raise StoryTaskStopped()
            if not paused and queue[0] == ticket:
                if stop_event.is_set():
                    raise StoryTaskStopped()
                return
        phase = "paused" if paused else "queued"
        if on_wait is not None and phase != last_phase:
            on_wait(phase, 0, 0, "排队任务已暂停" if paused else "正在等待本书前一个章纲或正文任务完成")
        last_phase = phase
        with _condition:
            _condition.wait(timeout=0.1)


@contextmanager
def story_task(ws_or_path, *, ticket=None, pause_event=None, stop_event=None, cancel_event=None, on_wait=None):
    """Reentrant book lease for worker/CLI entry points, using file_system identity.

    A path may be a book root or its file_system directory. UI code can reserve a
    ticket synchronously and pass it here from its worker. Cross-process callers
    are serialized by an OS lock outside this book's directory.
    """
    key = _book_key(ws_or_path)
    owned = getattr(_local, "books", set())
    if key in owned:
        yield
        return
    pause = pause_event or threading.Event()
    if pause_event is None:
        pause.set()
    stop = stop_event or threading.Event()
    lease = ticket or reserve_story_task(ws_or_path)
    if lease.book != key:
        raise ValueError("Story task ticket belongs to a different workspace")
    file_lock = FileLock(story_lock_path(ws_or_path))
    try:
        wait_for_story_task(lease, pause_event=pause, stop_event=stop, cancel_event=cancel_event, on_wait=on_wait)
        last_phase = None
        while True:
            if stop.is_set():
                raise StoryTaskStopped()
            if not pause.is_set():
                phase = "paused"
            elif file_lock.acquire():
                if stop.is_set():
                    raise StoryTaskStopped()
                break
            else:
                phase = "queued"
            # UI pause owns its cancellation flag; external CLI cancellation must
            # still interrupt a wait for a different process's lease.
            if pause.is_set():
                check_cancelled(cancel_event)
            if on_wait is not None and phase != last_phase:
                on_wait(phase, 0, 0, "排队任务已暂停" if phase == "paused" else "正在等待其他进程完成本书写作任务")
            last_phase = phase
            stop.wait(0.1)
        if not Path(key).parent.is_dir():
            raise FileNotFoundError("小说工作区已被删除，未重新创建或写入内容。")
        _local.books = owned | {key}
        yield
    finally:
        try:
            file_lock.release()
        finally:
            _local.books = owned
            release_story_task(lease)


def serialized_story_task(function):
    signature = inspect.signature(function)

    @wraps(function)
    def run(*args, **kwargs):
        arguments = signature.bind_partial(*args, **kwargs).arguments
        with story_task(arguments["ws"], pause_event=arguments.get("pause_event"),
                        stop_event=arguments.get("stop_event"), cancel_event=arguments.get("cancel_event"),
                        on_wait=arguments.get("progress_callback")):
            return function(*args, **kwargs)
    return run


@contextmanager
def idle_story_mutation(book_path):
    """Reject destructive UI changes while any worker is active or queued."""
    key = _book_key(book_path)
    with _condition:
        if _queues.get(key):
            raise StoryTaskBusy("本书仍有章纲或正文任务，请先结束任务再修改或重置。")
        ticket = reserve_story_task(book_path)
        _mutations.add(key)
    file_lock = FileLock(story_lock_path(book_path))
    try:
        if not file_lock.acquire():
            raise StoryTaskBusy("本书仍有章纲或正文任务，请先结束任务再修改或重置。")
        yield
    finally:
        try:
            file_lock.release()
        finally:
            with _condition:
                _mutations.discard(key)
                release_story_task(ticket)
