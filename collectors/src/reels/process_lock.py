"""Process ownership locks shared by collector, Android and analysis writers."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from .reel_records import isoformat_utc


def process_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            # Access denied means the process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class AtomicProcessLock:
    """A small cross-process lock for one JSON/CSV writer at a time."""

    _ownership_guard = threading.RLock()
    _active_owners: weakref.WeakValueDictionary[tuple[int, str], AtomicProcessLock] = weakref.WeakValueDictionary()

    def __init__(self, path: Path, *, wait_seconds: float = 0.0, stale_after_seconds: float | None = None) -> None:
        self.path = path.resolve()
        self.wait_seconds = max(0.0, wait_seconds)
        self.stale_after_seconds = stale_after_seconds
        self.acquired = False
        self._token = ""

    @property
    def _ownership_key(self) -> tuple[int, str]:
        return os.getpid(), os.path.normcase(str(self.path))

    def _remove_observed_owner(self, observed: bytes) -> bool:
        # Another process may have replaced an expired owner since our read.
        try:
            if self.path.read_bytes() != observed:
                return False
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.wait_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            with self._ownership_guard:
                if self.acquired:
                    raise RuntimeError(f"Lock is already acquired by this object: {self.path}")
                if self._ownership_key not in self._active_owners:
                    try:
                        observed = self.path.read_bytes()
                        owner = json.loads(observed)
                        owner_pid = int(owner.get("pid", 0))
                        if owner_pid <= 0:
                            raise ValueError("Lock record has no valid owner PID")
                    except FileNotFoundError:
                        self._token = uuid.uuid4().hex
                        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
                        try:
                            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                                json.dump({"pid": os.getpid(), "started_at": isoformat_utc(), "token": self._token}, file)
                                file.write("\n")
                            # Publish a complete record exclusively; readers never
                            # mistake an in-progress JSON write for a dead owner.
                            if os.name == "nt":
                                os.rename(temporary, self.path)  # Windows refuses to replace an existing file.
                            else:
                                os.link(temporary, self.path)
                            self._active_owners[self._ownership_key] = self
                            self.acquired = True
                            return True
                        except FileExistsError:
                            pass
                        except BaseException:
                            self.release()
                            raise
                        finally:
                            try:
                                Path(temporary).unlink(missing_ok=True)
                            except OSError as error:
                                print(f"Could not remove temporary lock record {temporary}: {error}", file=sys.stderr)
                    except (OSError, AttributeError, TypeError, ValueError):
                        # A legacy writer may still be creating its JSON record.
                        # An unreadable owner is not evidence that it is dead.
                        pass
                    else:
                        orphaned = owner_pid == os.getpid()
                        expired = False
                        if self.stale_after_seconds is not None:
                            try:
                                expired = time.time() - self.path.stat().st_mtime > self.stale_after_seconds
                            except FileNotFoundError:
                                continue
                        if orphaned or expired or not process_is_alive(owner_pid):
                            if self._remove_observed_owner(observed):
                                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def release(self) -> None:
        with self._ownership_guard:
            try:
                if not self._token:
                    return
                observed = self.path.read_bytes()
                owner = json.loads(observed)
                if owner.get("token") == self._token and int(owner.get("pid", 0)) == os.getpid():
                    self._remove_observed_owner(observed)
            except FileNotFoundError:
                pass
            except (OSError, AttributeError, TypeError, ValueError) as error:
                # Preserve the original write/export exception. A later acquire
                # in this process can reclaim our unregistered residual record.
                print(f"Could not release collector lock {self.path}: {error}", file=sys.stderr)
            finally:
                if self._active_owners.get(self._ownership_key) is self:
                    self._active_owners.pop(self._ownership_key, None)
                self.acquired = False
                self._token = ""

