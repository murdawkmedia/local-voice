import concurrent.futures
import importlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def test_new_ready_choice_keeps_dev5_settings_loader_compatible_for_rollback(self):
        self.settings.update(readyNotification=False)
        previous_defaults = {k: v for k, v in self.module.DEFAULTS.items() if k != 'readyNotification'}
        with patch.object(self.module, 'DEFAULTS', previous_defaults):
            self.assertTrue(self.module.Settings(self.path).get()['autoPaste'])
        self.assertFalse(self.module.Settings(self.path).get()['readyNotification'])

    def test_ready_cue_defaults_on_for_old_settings_and_opt_out_survives_restart(self):
        self.path.write_text('{"autoPaste": false}')
        self.assertTrue(self.module.Settings(self.path).get().get('readyNotification'))
        self.settings.update(readyNotification=False)
        self.settings.import_native_preferences({'preferences': {'readyNotification': True}})
        self.assertFalse(self.module.Settings(self.path).get()['readyNotification'])
        with self.assertRaises(ValueError): self.settings.update(readyNotification=1)

    def test_cleanup_defaults_on_for_existing_settings_and_can_be_disabled(self):
        self.assertTrue(self.settings.get().get('cleanFillers'))
        self.settings.update(cleanFillers=False)
        self.assertFalse(self.module.Settings(self.path).get()['cleanFillers'])
        self.settings.import_native_preferences({'preferences': {'cleanFillers': True}})
        self.assertFalse(self.settings.get()['cleanFillers'])

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.settings'),
                             'Private settings persistence is not implemented')
        self.module = importlib.import_module('local_voice.settings')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'settings.json'
        self.settings = self.module.Settings(self.path)

    def test_defaults_private_permissions_and_restart(self):
        defaults = self.settings.get()
        self.assertTrue(defaults['autoPaste'])
        self.assertFalse(defaults['hotkeyEnabled'])
        self.assertFalse(defaults['lowerPlayback'])
        self.assertTrue(defaults['meetingSystemAudio'])
        self.assertFalse(defaults['meetingSummary'])
        self.assertEqual(defaults['engine'], 'whisper')
        self.settings.update(microphoneSource='generated mic', autoPaste=False, engine='whisper')
        reopened = self.module.Settings(self.path).get()
        self.assertFalse(reopened['autoPaste'])
        self.assertEqual(reopened['microphoneSource'], 'generated mic')
        self.assertEqual(reopened['engine'], 'whisper')
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        defaults['hotkeyEnabled'] = True
        self.assertFalse(self.settings.get()['hotkeyEnabled'])

    def test_imported_preferences_cannot_replace_later_user_edits(self):
        preview = {'preferences': {'automaticPaste': False, 'mutePlayback': True,
                                   'meetingSystemAudio': False, 'meetingSummary': True,
                                   'access_token': 'NEVER COPY'}}
        self.settings.import_native_preferences(preview)
        self.assertFalse(self.settings.get()['autoPaste'])
        self.assertTrue(self.settings.get()['lowerPlayback'])
        self.assertFalse(self.settings.get()['meetingSystemAudio'])
        self.settings.update(autoPaste=True, lowerPlayback=False)
        self.module.Settings(self.path).import_native_preferences(preview)
        self.assertTrue(self.settings.get()['autoPaste'])
        self.assertFalse(self.settings.get()['lowerPlayback'])
        self.assertNotIn('NEVER COPY', self.path.read_text())
        self.assertNotIn('access_token', self.path.read_text())

    def test_user_choice_before_import_even_when_equal_to_default_is_preserved(self):
        self.settings.update(meetingSummary=False)
        self.settings.import_native_preferences({'preferences': {'meetingSummary': True}})
        self.assertFalse(self.settings.get()['meetingSummary'])

    def test_unknown_or_invalid_values_fail_without_partial_write(self):
        before = self.path.read_bytes()
        for fields in ({'secret': 'not accepted'}, {'autoPaste': 1}, {'engine': 'remote'},
                       {'language': ''}, {'microphoneSource': 'bad\x00source'}):
            with self.assertRaises(ValueError):
                self.settings.update(**fields)
            self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_old_file(self):
        before = self.path.read_bytes()
        with patch.object(self.module.os, 'replace', side_effect=OSError('generated failure')):
            with self.assertRaises(OSError):
                self.settings.update(autoPaste=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.settings-*')), [])

    def test_parallel_instances_preserve_unrelated_fields(self):
        other = self.module.Settings(self.path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.settings.update, microphoneSource='generated microphone'),
                       executor.submit(other.update, computerSource='generated monitor')]
            for future in futures:
                future.result()
        self.assertEqual(self.settings.get()['microphoneSource'], 'generated microphone')
        self.assertEqual(self.settings.get()['computerSource'], 'generated monitor')

    def test_malformed_existing_file_is_not_replaced(self):
        self.path.write_text('invalid json')
        with self.assertRaises(self.module.SettingsError):
            self.module.Settings(self.path)
        self.assertEqual(self.path.read_text(), 'invalid json')


if __name__ == '__main__':
    unittest.main()
