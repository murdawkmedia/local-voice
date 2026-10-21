"""Linux launcher owning one non-daemonizing worker and its process group.

Place namespace/credential-changing commands BEFORE this launcher and use
Popen(..., start_new_session=True). No preexec_fn runs in the threaded app.
The supervisor remains the app's direct child; parent death kills its group.
"""

import ctypes
import os
from pathlib import Path
import signal
import sys
import time


def guarded_command(argv, *, owner_pid=None):
    """Return a standalone launcher argv bound to the current app PID.

    Call in the app process, not in a background Python child. The owner PID
    is checked before and after installing the Linux parent-death signal.
    Workers must not change credentials, daemonize, or leave the owned group.
    """
    command = [os.fspath(arg) for arg in argv]
    if not command or any(not arg or "\0" in arg for arg in command):
        raise ValueError("A nonempty worker command is required")
    owner = os.getpid() if owner_pid is None else owner_pid
    if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0:
        raise ValueError("A valid owner PID is required")
    return [sys.executable, str(Path(__file__).resolve()), str(owner), "--", *command]


def _prctl(option, value):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, int(value), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "process guard unavailable")


def _parent_death_signal(sig):
    _prctl(1, sig)  # PR_SET_PDEATHSIG


def _reap_children():
    # Bound each pass even if a malfunctioning worker created many children.
    for _ in range(256):
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        except InterruptedError:
            continue
        if not pid:
            return False
    return False


def _cleanup_children():
    """Reap adopted descendants without killing the successful supervisor.

    Killing direct adopted children causes deeper descendants to be adopted
    in turn. An unreapable child (including kernel D-state) fails closed at
    the deadline; the caller then kills the entire owned group.
    """
    began = time.monotonic()
    deadline = began + .75
    os.killpg(os.getpid(), signal.SIGTERM)
    while True:
        if _reap_children():
            return True
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= began + .1:
            # Only the supervisor reaps its children, so each unreaped PID is
            # pinned against reuse between this snapshot and kill().
            with open(f"/proc/self/task/{os.getpid()}/children", "rb") as children:
                data = children.read(65536)
            if len(data) == 65536:
                return False
            for value in data.split():
                pid = int(value)
                if pid <= 1 or pid == os.getpid():
                    return False
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(.005)


def _launch(owner, command):
    if sys.platform != "linux" or os.getpgrp() != os.getpid() or os.getppid() != owner:
        return 125

    def owner_died(_sig, _frame):
        # This process is the verified leader of an exclusively owned group.
        os.killpg(os.getpid(), signal.SIGKILL)

    # SIGUSR1 distinguishes owner death from ordinary graceful group stops.
    signal.signal(signal.SIGUSR1, owner_died)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1, signal.SIGINT, signal.SIGTERM})
    _parent_death_signal(signal.SIGUSR1)
    _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER, before the worker can create helpers
    if os.getppid() != owner:  # closes death-before-prctl setup race
        return 125
    # The same-group worker receives these group signals directly. Keep the
    # supervisor alive until waitpid has reaped it, even when it ignores Stop.
    signal.signal(signal.SIGINT, lambda _sig, _frame: None)
    signal.signal(signal.SIGTERM, lambda _sig, _frame: None)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    supervisor = os.getpid()
    worker = os.fork()
    if worker == 0:
        try:
            _parent_death_signal(signal.SIGKILL)
            if os.getppid() != supervisor:
                os._exit(125)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
                signal.signal(sig, signal.SIG_DFL)
            os.execvp(command[0], command)
        except BaseException:
            os._exit(126)
    while True:
        try:
            _, status = os.waitpid(worker, 0)
            break
        except InterruptedError:
            continue
    code = os.waitstatus_to_exitcode(status)
    try:
        cleaned = _cleanup_children()
    except (OSError, ValueError):
        cleaned = False
    if not cleaned:
        # SIGKILL cannot force a task out of uninterruptible kernel I/O. It
        # does prevent further userspace work once that task becomes runnable.
        os.killpg(os.getpid(), signal.SIGKILL)
        return 125
    return code if code >= 0 else 128 - code


def main():
    try:
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            return 125
        return _launch(int(sys.argv[1]), sys.argv[3:])
    except BaseException:
        # Never print worker argv, private paths, stderr, or data.
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
