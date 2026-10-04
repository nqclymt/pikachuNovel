"""One cancellable, process-shared CLI slot per user, with a persistent circuit breaker."""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class SchedulerPaused(RuntimeError):
    def __init__(self, state):
        self.category = state.get("category", "process")
        super().__init__(state.get("message") or "Antigravity 调度已暂停，请在模型设置中恢复。")


def state_directory():
    override = os.getenv("HARNESS_NOVEL_AGY_STATE_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".harnessNovel" / "antigravity"


def check_cancelled(cancel_event=None):
    marker = os.getenv("HARNESS_NOVEL_CANCEL_FILE", "").strip()
    if (cancel_event is not None and cancel_event.is_set()) or (marker and Path(marker).is_file()):
        from core.llm_provider import LLMCallCancelled
        raise LLMCallCancelled("Antigravity 请求已取消。")


class FileLock:
    """Kernel locks are released automatically if the owning Python process exits."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            self.file.close()
            self.file = None
            return False
        except OSError as error:
            self.file.close()
            self.file = None
            if error.errno in (11, 13, 36):
                return False
            raise
        return True

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def _pause_state(root):
    try:
        state = json.loads((root / "pause.json").read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("paused"), bool):
            return state
    except FileNotFoundError:
        return {"paused": False, "category": "", "message": "", "paused_at": None}
    except (ValueError, OSError):
        pass
    return {"paused": True, "category": "process", "message": "Antigravity 调度状态无法读取，请检查目录权限并点击恢复调度。", "paused_at": None}


def _save_pause(root, state):
    root.mkdir(parents=True, exist_ok=True)
    pending = root / ("pause-" + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(pending), str(root / "pause.json"))
    finally:
        pending.unlink(missing_ok=True)


def pause_scheduler(category, message):
    _save_pause(state_directory(), {"paused": True, "category": category, "message": message, "paused_at": time.time()})


def _waiters(root):
    alive = []
    now = time.time()
    for ticket in (root / "queue").glob("*.wait"):
        try:
            # Stopped processes cannot leave an immortal queue entry.
            if now - ticket.stat().st_mtime < 10:
                alive.append(ticket.name)
        except FileNotFoundError:
            pass
    return sorted(alive)


def scheduler_status():
    root = state_directory()
    state = _pause_state(root)
    lock = FileLock(root / "run.lock")
    running = not lock.acquire()
    lock.release()
    waiting = len(_waiters(root))
    return dict(state, running=running, waiting=waiting, max_concurrency=1,
                reason=state.get("message", ""), active=int(running), queued=waiting)


def resume_scheduler():
    _save_pause(state_directory(), {"paused": False, "category": "", "message": "", "paused_at": None})
    return scheduler_status()


@contextmanager
def scheduler_slot(cancel_event=None, status_callback=None):
    root = state_directory()
    queue = root / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    ticket = queue / f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex}.wait"
    ticket.touch()
    lock = FileLock(root / "run.lock")
    notified = False
    heartbeat = 0.0
    try:
        while True:
            check_cancelled(cancel_event)
            state = _pause_state(root)
            if state.get("paused"):
                raise SchedulerPaused(state)
            if time.monotonic() - heartbeat > 1:
                ticket.touch()
                heartbeat = time.monotonic()
            waiting = _waiters(root)
            if waiting and waiting[0] == ticket.name and lock.acquire():
                # A previous holder may have paused between our read and acquire.
                state = _pause_state(root)
                if state.get("paused"):
                    raise SchedulerPaused(state)
                check_cancelled(cancel_event)
                ticket.unlink(missing_ok=True)
                break
            if not notified and status_callback:
                status_callback("Antigravity 请求正在排队；同一用户一次只运行一个请求。")
                notified = True
            time.sleep(0.1)
        yield
    finally:
        lock.release()
        ticket.unlink(missing_ok=True)
