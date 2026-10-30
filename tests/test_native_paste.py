import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from local_voice.desktop import DesktopOutput, DesktopError, run_command


def process_start():
    return int(Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19])


class NativePasteTests(unittest.TestCase):
    def setUp(self):
        self.target = {'address': '0x123', 'class': 'GeneratedEditor',
                       'pid': os.getpid(), 'start': process_start()}
        self.window = dict(self.target, mapped=True, hidden=False, visible=True,
                           acceptsInput=True, xwayland=False)
        self.calls = []
        self.no_focus = False
        self.ack = b'ok\n'
        self.dispatch_error = False

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:3] == ['hyprctl', '-j', 'activewindow']:
            return json.dumps(self.window).encode()
        if argv[:3] == ['hyprctl', '-j', 'getprop']:
            return json.dumps({'no_focus': self.no_focus}).encode()
        if argv[:2] == ['hyprctl', 'dispatch']:
            if self.dispatch_error:
                raise DesktopError('command-failed')
            return self.ack
        return b''

    def output(self, modifiers=lambda: False):
        return DesktopOutput(runner=self.runner, modifiers_held=modifiers, modifier_timeout=0)

    def injections(self):
        return [a for a, _ in self.calls if a[:2] == ['hyprctl', 'dispatch'] or a[0] == 'wtype']

    def test_native_paste_targets_exact_window_without_virtual_keyboard_or_text_arguments(self):
        result = self.output().deliver('Generated phrase', self.target)
        self.assertTrue(result['pasted'])
        self.assertEqual(len(self.injections()), 1)
        args = self.injections()[0]
        self.assertEqual(args[:2], ['hyprctl', 'dispatch'])
        expression = args[2]
        for required in ('hl.get_active_window()', 'w.address', 'w.pid',
                         'w.mapped', 'w.hidden', 'w.accepts_input',
                         'hl.dsp.send_shortcut', 'address:0x123'):
            self.assertIn(required, expression)
        self.assertNotIn('Generated phrase', str(args))
        self.assertNotIn('Return', expression)
        self.assertNotIn('Enter', expression)
        self.assertEqual(self.calls[0][1]['input'], b'Generated phrase')
        options = next(o for a, o in self.calls if a == args)
        self.assertLessEqual(options['timeout'], 2)
        self.assertLessEqual(options['maximum'], 4096)

    def test_focus_identity_tracks_process_lifetime(self):
        self.assertEqual(self.output().focus(), self.target)
        for key, value in [('pid', os.getpid()+10000000), ('start', process_start()+1),
                           ('address', '0x999'), ('class', 'Changed')]:
            with self.subTest(key=key):
                self.calls.clear()
                result = self.output().deliver('Generated', dict(self.target, **{key: value}))
                self.assertFalse(result['pasted'])
                self.assertFalse(self.injections())

    def test_focus_switch_during_modifier_probe_never_injects(self):
        def switch():
            self.window['address'] = '0x456'
            return False
        result = self.output(switch).deliver('Generated', self.target)
        self.assertFalse(result['pasted'])
        self.assertFalse(self.injections())

    def test_held_unknown_and_failed_physical_modifier_checks_never_inject(self):
        def unavailable():raise OSError('Generated input failure')
        for check in [lambda: True, lambda: None, unavailable]:
            with self.subTest(check=check):
                self.calls.clear()
                result = self.output(check).deliver('Generated', self.target)
                self.assertTrue(result['copied'])
                self.assertFalse(result['pasted'])
                self.assertFalse(self.injections())

    def test_final_modifier_recheck_never_injects_after_new_key(self):
        states=iter([False, True])
        result=self.output(lambda:next(states)).deliver('Generated',self.target)
        self.assertFalse(result['pasted']);self.assertFalse(self.injections())

    def test_no_focus_hidden_unmapped_unavailable_or_xwayland_target_never_injects(self):
        for key, value in [('acceptsInput',False),('mapped',False),('hidden',True),
                           ('visible',False),('xwayland',True)]:
            old=self.window[key];self.window[key]=value;self.calls.clear()
            self.assertFalse(self.output().deliver('Generated',self.target)['pasted'])
            self.assertFalse(self.injections());self.window[key]=old
        self.no_focus=True;self.calls.clear()
        self.assertFalse(self.output().deliver('Generated',self.target)['pasted'])
        self.assertFalse(self.injections())

    def test_invalid_address_or_process_data_never_enters_lua_or_arguments(self):
        for address in ['0x0', '0x1");os.execute("bad")--', 'address:0x123',
                        '0x123;exec bad', '0x'+'a'*17, '--batch', '0x12\n3']:
            self.calls.clear()
            self.assertFalse(self.output().deliver('Generated',dict(self.target,address=address))['pasted'])
            self.assertFalse(self.injections())
            self.assertNotIn(address,[str(a) for a,_ in self.calls if a[0]=='hyprctl'])
        for pid in [True,-1,'1',2**40]:
            self.calls.clear()
            self.assertFalse(self.output().deliver('Generated',dict(self.target,pid=pid))['pasted'])
            self.assertFalse(self.injections())

    def test_unsupported_or_error_transport_keeps_copy_without_retry_or_other_injection(self):
        for ack in [b'unsupported',b'',b'ok but uncertain',b'error: focus changed']:
            self.calls.clear();self.ack=ack
            result=self.output().deliver('Generated',self.target)
            self.assertTrue(result['copied']);self.assertFalse(result['pasted'])
            self.assertEqual(len(self.injections()),1)
            self.assertNotEqual(self.injections()[0][0],'wtype')
        self.calls.clear();self.dispatch_error=True
        result=self.output().deliver('Generated',self.target)
        self.assertFalse(result['pasted']);self.assertEqual(len(self.injections()),1)

    def test_native_command_timeout_and_output_are_bounded(self):
        with self.assertRaises(DesktopError):
            run_command([sys.executable,'-c','import time;time.sleep(5)'],timeout=.05)
        with self.assertRaises(DesktopError):
            run_command([sys.executable,'-c','print("x"*10000)'],maximum=64)

    def test_unknown_no_focus_policy_is_not_treated_as_false(self):
        for value in [0, None, '', 'false', []]:
            self.no_focus=value;self.calls.clear()
            self.assertFalse(self.output().deliver('Generated',self.target)['pasted'])
            self.assertFalse(self.injections())

    def test_application_class_is_never_interpolated_into_lua(self):
        app='Generated\"; os.execute(\"untrusted\"); --'
        self.target['class']=app;self.window['class']=app
        self.assertTrue(self.output().deliver('Generated',self.target)['pasted'])
        self.assertNotIn(app,self.injections()[0][2])
        self.assertNotIn('os.execute',self.injections()[0][2])
