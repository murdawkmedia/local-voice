import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


class DesktopTests(unittest.TestCase):
    def test_ready_notification_is_silent_transient_bounded_and_never_focuses(self):
        output = self.module.DesktopOutput(runner=self.runner)
        output.notify_ready()
        self.assertEqual(len(self.calls), 1)
        argv, options = self.calls[0]
        self.assertEqual(argv[0], 'notify-send')
        self.assertIn('Listening', argv)
        self.assertIn('--hint=boolean:suppress-sound:true', argv)
        self.assertIn('--transient', argv)
        self.assertIn('--expire-time=1500', argv)
        self.assertNotIn('--wait', argv)
        self.assertNotIn('--action', argv)
        self.assertEqual(options, {'capture': False, 'timeout': 1})

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.desktop'),
                             'Guarded desktop integration is not implemented')
        self.module = importlib.import_module('local_voice.desktop')
        self.calls = []
        self.target = {'address': '0x123', 'class': 'GeneratedEditor'}

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == 'hyprctl':
            return json.dumps(self.target).encode()
        return b''

    def test_copy_uses_stdin_and_never_reads_clipboard(self):
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: False)
        output.copy('Generated sensitive phrase')
        argv, options = self.calls[0]
        self.assertEqual(argv, ['wl-copy', '--type', 'text/plain;charset=utf-8'])
        self.assertEqual(options['input'], b'Generated sensitive phrase')
        self.assertNotIn('Generated sensitive phrase', ' '.join(argv))

    def test_delivery_copies_then_pastes_only_matching_focus_without_enter(self):
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: False)
        self.assertEqual(output.focus(), self.target)
        result = output.deliver('Generated phrase', dict(self.target))
        self.assertTrue(result['copied'])
        self.assertTrue(result['pasted'])
        commands = [call[0] for call in self.calls]
        self.assertEqual(commands[-1], ['wtype', '-M', 'ctrl', '-k', 'v', '-m', 'ctrl'])
        self.assertNotIn('Return', str(commands))
        self.assertNotIn('Enter', str(commands))

    def test_changed_address_or_class_retains_copy_without_key_injection(self):
        for captured in (None, {'address': '0x999', 'class': self.target['class']},
                         {'address': self.target['address'], 'class': 'OtherClass'}):
            self.calls.clear()
            output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: False)
            result = output.deliver('Generated phrase', captured)
            self.assertTrue(result['copied'])
            self.assertFalse(result['pasted'])
            self.assertNotIn('wtype', [call[0][0] for call in self.calls])

    def test_disabled_auto_paste_and_paste_failure_keep_clipboard(self):
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: False)
        result = output.deliver('Generated phrase', self.target, auto_paste=False)
        self.assertEqual(result['reason'], 'copied')
        self.assertEqual(len(self.calls), 1)
        def failing(argv, **kwargs):
            if argv[0] == 'wtype':
                raise self.module.DesktopError('paste-unavailable')
            return self.runner(argv, **kwargs)
        result = self.module.DesktopOutput(runner=failing, modifiers_held=lambda: False).deliver('Generated', self.target)
        self.assertTrue(result['copied'])
        self.assertFalse(result['pasted'])
        self.assertEqual(result['reason'], 'paste-unavailable')

    def test_clipboard_failure_reports_saved_text_without_paste(self):
        def unavailable(argv, **kwargs):
            self.calls.append((argv, kwargs))
            raise self.module.DesktopError('unavailable')
        result = self.module.DesktopOutput(runner=unavailable).deliver('Generated', self.target)
        self.assertFalse(result['copied'])
        self.assertFalse(result['pasted'])
        self.assertEqual([call[0][0] for call in self.calls], ['wl-copy'])

    def test_terminal_paste_includes_shift_and_modifiers_can_block_paste(self):
        self.target['class'] = 'com.mitchellh.ghostty'
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: False)
        output.deliver('Generated', self.target)
        self.assertEqual(self.calls[-1][0], ['wtype', '-M', 'ctrl', '-M', 'shift', '-k', 'v', '-m', 'shift', '-m', 'ctrl'])
        self.calls.clear()
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=lambda: True,
                                           modifier_timeout=0.01)
        result = output.deliver('Generated', self.target)
        self.assertEqual(result['reason'], 'modifiers-held')
        self.assertNotIn('wtype', [call[0][0] for call in self.calls])

    def test_focus_is_checked_again_after_modifier_wait(self):
        checks = []
        def changes_focus():
            checks.append(1)
            self.target = {'address': '0x456', 'class': 'OtherEditor'}
            return False
        output = self.module.DesktopOutput(runner=self.runner, modifiers_held=changes_focus)
        result = output.deliver('Generated', {'address': '0x123', 'class': 'GeneratedEditor'})
        self.assertEqual(result['reason'], 'focus-changed')
        self.assertFalse(result['pasted'])

    def test_device_inventory_is_local_readonly_and_labels_monitors_and_bluetooth(self):
        responses = {
            'sources': [
                {'index': 1, 'name': 'generated_mic', 'description': 'Generated Microphone', 'monitor_of_sink': 4294967295},
                {'index': 2, 'name': 'bluez_output.generated.monitor', 'description': 'Generated Monitor', 'monitor_of_sink': 9}],
            'sinks': [{'index': 9, 'name': 'bluez_output.generated', 'description': 'Generated Speaker',
                       'properties': {'device.bus': 'bluetooth'}}]}
        def runner(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if 'list' in argv:
                return json.dumps(responses[argv[-1]]).encode()
            return (b'generated_mic\n' if argv[-1] == 'get-default-source' else b'bluez_output.generated\n')
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000'}, clear=True):
            devices = self.module.pulse_devices(runner=runner)
        self.assertFalse(devices['sources'][0]['monitor'])
        self.assertTrue(devices['sources'][1]['monitor'])
        self.assertTrue(devices['sources'][1]['bluetooth'])
        self.assertEqual(devices['sinks'][0]['monitor'], 'bluez_output.generated.monitor')
        self.assertEqual(devices['defaultSource'], 'generated_mic')
        for argv, kwargs in self.calls:
            self.assertIn('--server=unix:/run/user/1000/pulse/native', argv)
            self.assertNotIn('set-', ' '.join(argv))

    def test_remote_pulse_server_is_rejected_without_running_command(self):
        with patch.dict(os.environ, {'PULSE_SERVER': 'tcp:remote.invalid'}, clear=True):
            with self.assertRaises(self.module.DesktopError):
                self.module.pulse_devices(runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_source_selection_requires_a_real_monitor_for_computer_audio(self):
        devices = {'defaultSource': 'mic', 'sources': [
            {'name': 'mic', 'monitor': False}, {'name': 'out.monitor', 'monitor': True}]}
        with self.assertRaises(self.module.DesktopError):
            self.module.select_sources(devices, meeting=True)
        with self.assertRaises(self.module.DesktopError):
            self.module.select_sources(devices, computer_source='mic', meeting=True)
        self.assertEqual(self.module.select_sources(devices, computer_source='out.monitor', meeting=True),
                         {'microphone': 'mic', 'computer': 'out.monitor'})
        self.assertEqual(self.module.select_sources(devices, meeting=True, include_system=False), {'microphone': 'mic'})

    def test_blank_computer_source_follows_each_cached_default_output(self):
        devices = {'defaultSource': 'mic', 'defaultSink': 'first', 'sources': [
            {'name': 'mic', 'monitor': False}, {'name': 'first.monitor', 'monitor': True},
            {'name': 'second.monitor', 'monitor': True}], 'sinks': [
            {'name': 'first', 'monitor': 'first.monitor'}, {'name': 'second', 'monitor': 'second.monitor'}]}
        for sink in ('first', 'second'):
            inventory = dict(devices, defaultSink=sink)
            self.assertEqual(self.module.select_sources(inventory, meeting=True),
                             {'microphone': 'mic', 'computer': sink + '.monitor'})
            self.assertEqual(self.module.select_sources(inventory, computer_source='first.monitor', meeting=True),
                             {'microphone': 'mic', 'computer': 'first.monitor'})

    def test_missing_default_monitor_and_missing_override_never_use_another_monitor(self):
        devices = {'defaultSource': 'mic', 'defaultSink': 'absent', 'sources': [
            {'name': 'mic', 'monitor': False}, {'name': 'other.monitor', 'monitor': True}],
            'sinks': [{'name': 'other', 'monitor': 'other.monitor'}]}
        for inventory in (devices, dict(devices, defaultSink='broken', sinks=[
                {'name': 'broken', 'monitor': 'missing.monitor'}]),
                dict(devices, defaultSink='broken', sinks=[{'name': 'broken', 'monitor': 'mic'}])):
            with self.assertRaises(self.module.DesktopError):
                self.module.select_sources(inventory, meeting=True)
        with self.assertRaises(self.module.DesktopError):
            self.module.select_sources(dict(devices, defaultSink='other'),
                                       computer_source='missing.monitor', meeting=True)

    def test_command_output_and_duration_are_bounded(self):
        with self.assertRaises(self.module.DesktopError):
            self.module.run_command([sys.executable, '-c', 'import sys; sys.stdout.write("x"*10000)'], maximum=128)
        start = time.monotonic()
        with self.assertRaises(self.module.DesktopError):
            self.module.run_command([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)

    def test_failed_modifier_probe_keeps_copy_without_pasting(self):
        def unavailable():
            raise OSError('generated missing input permission')
        result = self.module.DesktopOutput(runner=self.runner, modifiers_held=unavailable).deliver('Generated', self.target)
        self.assertTrue(result['copied'])
        self.assertFalse(result['pasted'])
        self.assertEqual(result['reason'], 'modifiers-unavailable')

    def test_timed_out_command_stops_owned_descendant_after_leader_exits(self):
        import signal
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / 'generated-pid'
            script = ('import pathlib,subprocess,sys; '
                      'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"]); '
                      'pathlib.Path(sys.argv[1]).write_text(str(child.pid))')
            with self.assertRaises(self.module.DesktopError):
                self.module.run_command([sys.executable, '-c', script, str(pid_path)], timeout=0.15)
            child_pid = int(pid_path.read_text())
            try:
                state_file = Path('/proc') / str(child_pid) / 'stat'
                deadline = time.monotonic() + 0.3
                active = True
                while active and time.monotonic() < deadline:
                    active = state_file.exists() and state_file.read_text().split()[2] != 'Z'
                    if active:
                        time.sleep(0.005)
                self.assertFalse(active, 'Timed-out command left its own descendant running')
            finally:
                try:
                    command = (Path('/proc') / str(child_pid) / 'cmdline').read_bytes()
                    if b'import time; time.sleep(20)' in command:
                        os.kill(child_pid, signal.SIGKILL)
                except (ProcessLookupError, FileNotFoundError):
                    pass


if __name__ == '__main__':
    unittest.main()
