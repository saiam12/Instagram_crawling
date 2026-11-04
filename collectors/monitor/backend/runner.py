"""Own one UI-started task; a stop file requests the collector's SIGINT handler."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from state import BASE, COLLECTORS, now, read_json, write_json


def main():
    identifier = sys.argv[1]
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid run ID")
    path = BASE.parent / "runtime" / "monitor" / "runs" / f"{identifier}.json"
    job = read_json(path)
    if job.get("state") != "queued":
        raise ValueError("Task is not queued")
    job.update(pid=os.getpid(), started_at=now(), state="starting")
    write_json(path, job)
    done = threading.Event()
    stop_requested = threading.Event()
    sys.path.insert(0, str(COLLECTORS))

    def watch_stop():
        while not done.wait(0.5):
            if path.with_suffix(".stop").exists():
                stop_requested.set()
                try:
                    job["state"] = "stopping"
                    write_json(path, job)
                    # Stop only this run's queues; never terminate unrelated processes.
                    from src.reels.instagram_reels_browser import request_collection_stop
                    domains = ("fashion", "beauty") if job["command"] == "fashion-beauty" else (job["command"],)
                    for domain in domains:
                        request_collection_stop(Path(job["data_root"]) / ".datasets" / domain,
                                                source="monitor", reason="사용자가 웹 화면에서 중지를 요청했습니다.", consecutive_failures=0)
                except Exception as exception:
                    job["stop_error"] = f"일부 중지 신호를 저장하지 못했습니다. Android 상태를 확인하세요: {exception}"
                    traceback.print_exc()
                finally:
                    signal.raise_signal(signal.SIGINT)
                return

    code, error = 1, ""
    try:
        if job["kind"] == "android" or job["mode"] == "hybrid":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                       str(COLLECTORS / "scripts" / "android" / "start-android.ps1")]
            if job.get("avd"):
                command += ["-Avd", job["avd"]]
            if job.get("device"):
                command += ["-DeviceId", job["device"]]
                # Child PowerShell environment changes cannot reach this runner.
                os.environ["INSTAGRAM_ANDROID_DEVICE_ID"] = job["device"]
            command += ["-NoWindow"] if job["kind"] == "collector" else []
            subprocess.run(command, check=True, timeout=240,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if job["kind"] == "collector":
            from scripts.instagram_reels_python import main as collect
            job["state"] = "running"
            write_json(path, job)
            threading.Thread(target=watch_stop, daemon=True).start()
            code = int(collect(job["args"]) or 0)
        else:
            code = 0
    except KeyboardInterrupt:
        code = 130
    except SystemExit as exception:
        code = exception.code if isinstance(exception.code, int) else 1
    except Exception as exception:
        error = str(exception)
        traceback.print_exc()
    finally:
        done.set()
        job.update(state="stopped" if stop_requested.is_set() else "completed" if code == 0 else "failed",
                   exit_code=code, finished_at=now(), error=error or job.get("stop_error", ""))
        write_json(path, job)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
