"""Same-user, bounded UNIX control messages. No transcript transport."""

import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading


COMMANDS = frozenset({'status', 'start', 'meeting', 'stop', 'activate', 'quit'})


class AlreadyRunning(RuntimeError):
    pass


class Instance:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'control.sock'
        self._lock = None
        self._server = None
        self._thread = None
        self._closed = threading.Event()
        self._socket_identity = None

    def __enter__(self):
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise ValueError('Control directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_uid != os.getuid():
            raise ValueError('Control directory must belong to this user')
        self.directory.chmod(0o700)
        self._lock = os.open(self.directory/'app.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self._lock).st_mode):
                raise ValueError('Unsupported application lock')
            os.fchmod(self._lock, 0o600)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning() from None
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError('Refusing to remove an unrelated control path')
                self.path.unlink()
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self.path))
            self.path.chmod(0o600)
            info = self.path.stat()
            self._socket_identity = (info.st_dev, info.st_ino)
            self._server.listen(4)
            self._server.settimeout(.2)
            return self
        except BaseException:
            if self._server is not None:
                self._server.close()
            os.close(self._lock)
            self._lock = None
            raise

    def serve(self, handler):
        if self._thread is not None:
            raise RuntimeError('Control server already started')
        self._thread = threading.Thread(target=self._run, args=(handler,), name='voice-control', daemon=True)
        self._thread.start()

    def _run(self, handler):
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(1)
                try:
                    _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                    request = _read(client, 4096)
                    command = request.get('command')
                    if request.keys() != {'command'} or command not in COMMANDS:
                        result = {'ok': False, 'message': 'Unknown control command.'}
                    else:
                        result = handler(command)
                    payload = json.dumps(result, allow_nan=False).encode()
                    if len(payload) > 65536:
                        payload = b'{"ok":false,"message":"Control result is too large."}'
                    client.sendall(payload + b'\n')
                except (OSError, ValueError, TypeError, AttributeError):
                    continue

    def close(self):
        self._closed.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        try:
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) == self._socket_identity and stat.S_ISSOCK(info.st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def __exit__(self, *args):
        self.close()


def _read(client, maximum):
    data = bytearray()
    while b'\n' not in data:
        block = client.recv(min(4096, maximum + 1 - len(data)))
        if not block:
            raise ValueError('Incomplete control message')
        data.extend(block)
        if len(data) > maximum:
            raise ValueError('Control message exceeds its limit')
    if data.count(b'\n') != 1 or not data.endswith(b'\n'):
        raise ValueError('One control message is required')
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError('Expected a control object')
    return result


def send_command(directory, command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(Path(directory)/'control.sock'))
            client.sendall(json.dumps({'command': command}).encode() + b'\n')
            return _read(client, 65536)
    except (OSError, ValueError):
        return {'ok': False, 'message': 'Local Voice is not running or did not answer.'}
