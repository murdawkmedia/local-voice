"""Bounded local desktop reads and focus-guarded clipboard delivery."""

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


class DesktopError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def run_command(argv, *, input=None, env=None, timeout=3, maximum=1_048_576, capture=True):
    """No shell, bounded stdout/time, no retained stderr or transcript arguments."""
    if not math.isfinite(timeout) or not 0 < timeout <= 10 or not 0 <= maximum <= 4_194_304:
        raise ValueError('Invalid command bounds')
    if input is not None and (not isinstance(input, bytes) or len(input) > 1_048_576):
        raise DesktopError('input-limit')
    process = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    offset = 0
    successful = False
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        if process.stdout is not None:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
        if process.stdin is not None:
            if input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopError('command-timeout')
            if not selector.get_map():
                process.wait(timeout=remaining)
                break
            for key, events in selector.select(min(remaining, 0.1)):
                if events & selectors.EVENT_READ:
                    try:
                        data = os.read(key.fd, min(65_536, maximum + 1 - len(output)))
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        output.extend(data)
                        if len(output) > maximum:
                            raise DesktopError('output-limit')
                else:
                    try:
                        offset += os.write(key.fd, input[offset:offset + 65_536])
                    except BlockingIOError:
                        continue
                    if offset == len(input):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
        if process.wait(timeout=max(0.001, deadline - time.monotonic())):
            raise DesktopError('command-failed')
        successful = True
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise DesktopError('command-unavailable') from None
    finally:
        selector.close()
        if process is not None:
            if not successful:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def validate_pulse_server(server):
    if not isinstance(server, str):
        raise ValueError('A local UNIX audio server is required')
    if server.startswith('/'):
        server = 'unix:' + server
    if (not server.startswith('unix:/') or '\x00' in server or ';' in server or
            any(c.isspace() for c in server)):
        raise ValueError('A local UNIX audio server is required')
    return server


def _pulse_server():
    server = os.environ.get('PULSE_SERVER', '')
    if not server:
        runtime = os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
        server = 'unix:' + runtime + '/pulse/native'
    try:
        return validate_pulse_server(server)
    except ValueError:
        raise DesktopError('local-pulse-required') from None


def _bluetooth(item):
    props = item.get('properties', {})
    if not isinstance(props, dict):
        props = {}
    return (item['name'].startswith(('bluez_input.', 'bluez_output.'))
            or props.get('device.bus') == 'bluetooth' or props.get('device.api') == 'bluez5'
            or 'api.bluez5.address' in props)


def pulse_devices(*, runner=run_command):
    server = _pulse_server()
    prefix = ['pactl', '--server=' + server]
    env = dict(os.environ, PULSE_SERVER=server)
    try:
        sources = json.loads(runner(prefix + ['-f', 'json', 'list', 'sources'], env=env))
        sinks = json.loads(runner(prefix + ['-f', 'json', 'list', 'sinks'], env=env))
        if not isinstance(sources, list) or not isinstance(sinks, list) or len(sources) + len(sinks) > 512:
            raise ValueError()
        for item in sources + sinks:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
                raise ValueError()
        clean_sources = []
        for item in sources:
            monitor = item.get('monitor_of_sink') not in (None, -1, 4294967295, 'n/a') or item['name'].endswith('.monitor')
            clean_sources.append(dict(name=item['name'], description=item.get('description') or item['name'],
                                      monitor=monitor, bluetooth=_bluetooth(item)))
        clean_sinks = []
        for item in sinks:
            monitor = next((source['name'] for source in sources
                            if source.get('monitor_of_sink') == item.get('index')
                            and item.get('index') is not None), '')
            if not monitor:
                monitor = next((source['name'] for source in sources if source['name'] == item['name'] + '.monitor'), '')
            clean_sinks.append(dict(name=item['name'], description=item.get('description') or item['name'],
                                    monitor=monitor, bluetooth=_bluetooth(item)))
        default_source = runner(prefix + ['get-default-source'], env=env, maximum=4096).decode('utf-8').strip()
        default_sink = runner(prefix + ['get-default-sink'], env=env, maximum=4096).decode('utf-8').strip()
        return dict(sources=clean_sources, sinks=clean_sinks, defaultSource=default_source, defaultSink=default_sink)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DesktopError('invalid-device-inventory') from None


def select_sources(devices, microphone_source='', computer_source='', meeting=False, include_system=True):
    """Select from a cached inventory; never probe devices on the recording path."""
    sources = {item['name']: item for item in devices.get('sources', [])}
    microphone = microphone_source or devices.get('defaultSource', '')
    if microphone not in sources or sources[microphone].get('monitor'):
        raise DesktopError('missing-microphone-source')
    result = {'microphone': microphone}
    if meeting and include_system:
        if not computer_source:
            speaker = next((item for item in devices.get('sinks', [])
                            if item['name'] == devices.get('defaultSink')), None)
            computer_source = speaker.get('monitor', '') if speaker else ''
        if not computer_source or computer_source not in sources or not sources[computer_source].get('monitor'):
            raise DesktopError('missing-computer-source')
        result['computer'] = computer_source
    return result


def physical_modifiers_held():
    """Fail closed when keyboard modifier state cannot be established."""
    try:
        import evdev
    except ImportError:
        return True
    checked = 0
    modifiers = {29, 97, 42, 54, 56, 100, 125, 126}
    try:
        paths = evdev.list_devices()
        if len(paths) > 64:
            return True
        for path in paths:
            try:
                device = evdev.InputDevice(path)
                try:
                    if not modifiers.intersection(device.capabilities().get(1, [])):
                        continue
                    checked += 1
                    if modifiers.intersection(device.active_keys()):
                        return True
                finally:
                    device.close()
            except OSError:
                return True
    except OSError:
        return True
    return checked == 0


_TERMINALS = ('alacritty', 'ghostty', 'kitty', 'foot', 'wezterm', 'xterm', 'gnome-terminal',
              'org.gnome.terminal', 'blackbox', 'konsole', 'org.omarchy.terminal')


class DesktopOutput:
    def __init__(self, *, runner=run_command, modifiers_held=physical_modifiers_held, modifier_timeout=0.3):
        if not 0 <= modifier_timeout <= 1:
            raise ValueError('Invalid modifier wait')
        self.runner = runner
        self.modifiers_held = modifiers_held
        self.modifier_timeout = modifier_timeout

    def notify_ready(self):
        self.runner(['notify-send', '--app-name=Local Voice', '--transient',
                     '--expire-time=1500', '--hint=boolean:suppress-sound:true',
                     'Listening', 'Speak now. Release Ctrl + Super to transcribe.'],
                    capture=False, timeout=1)

    def copy(self, text):
        if not isinstance(text, str):
            raise ValueError('Expected transcript text')
        self.runner(['wl-copy', '--type', 'text/plain;charset=utf-8'], input=text.encode('utf-8'), capture=False)
        return True

    def focus(self):
        try:
            value = json.loads(self.runner(['hyprctl', '-j', 'activewindow'], maximum=65_536))
            if not isinstance(value, dict):
                return None
            address, app = value.get('address'), value.get('class')
            if (not isinstance(address, str) or not address or address == '0x0' or len(address) > 128
                    or not isinstance(app, str) or not app or len(app) > 512):
                return None
            return {'address': address, 'class': app}
        except (DesktopError, ValueError, TypeError, OSError):
            return None

    def deliver(self, text, captured_focus, auto_paste=True):
        result = {'copied': False, 'pasted': False, 'reason': 'clipboard-unavailable'}
        try:
            self.copy(text)
        except (DesktopError, OSError):
            return result
        result['copied'] = True
        result['reason'] = 'copied'
        if not auto_paste:
            return result
        target = captured_focus
        if (not isinstance(target, dict) or not isinstance(target.get('address'), str)
                or not isinstance(target.get('class'), str) or not target['address'] or not target['class']):
            result['reason'] = 'missing-focus'
            return result
        target = {'address': target['address'], 'class': target['class']}
        if self.focus() != target:
            result['reason'] = 'focus-changed'
            return result
        deadline = time.monotonic() + self.modifier_timeout
        while True:
            try:
                held = self.modifiers_held()
                if type(held) is not bool:
                    raise ValueError('Unknown modifier state')
            except Exception:
                result['reason'] = 'modifiers-unavailable'
                return result
            if not held:
                break
            if time.monotonic() >= deadline:
                result['reason'] = 'modifiers-held'
                return result
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))
        if self.focus() != target:
            result['reason'] = 'focus-changed'
            return result
        try:
            if self.modifiers_held() is not False:
                result['reason'] = 'modifiers-held'
                return result
        except Exception:
            result['reason'] = 'modifiers-unavailable'
            return result
        argv = ['wtype', '-M', 'ctrl']
        terminal = any(name in target['class'].casefold() for name in _TERMINALS)
        if terminal:
            argv += ['-M', 'shift']
        argv += ['-k', 'v']
        if terminal:
            argv += ['-m', 'shift']
        argv += ['-m', 'ctrl']
        try:
            self.runner(argv, capture=False)
            result.update(pasted=True, reason='pasted')
        except (DesktopError, OSError):
            result['reason'] = 'paste-unavailable'
        return result
