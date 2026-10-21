import copy
import json
from pathlib import Path
import tempfile
import unittest

from local_voice.playback import PlaybackDucker


class Pulse:
    def __init__(self):
        self.sinks = [{'index': 1, 'name': 'generated-speaker',
                       'volume': {'left': {'value': 60000}, 'right': {'value': 40000}},
                       'properties': {'device.serial': 'generated-physical-output'}}]
        self.streams = [{'index': 7, 'client': 3, 'volume': {'mono': {'value': 50000}},
                         'properties': {'application.name': 'Generated player'}}]
        self.gains = {1: 1.0}
        self.mutations = []
        self.fail_restore = False
        self.ignore_setter = False
        self.round_readback = False

    def __call__(self, argv, **kwargs):
        if argv[0] == 'pw-dump':
            return json.dumps([{'id': row['index'], 'type': 'PipeWire:Interface:Node', 'info': {
                'props': row['properties'] | {'media.class': 'Audio/Sink', 'node.name': row['name']},
                'params': {'Props': [{'volume': float(format(self.gains[row['index']], '.6g'))
                                                  if self.round_readback else self.gains[row['index']]}]}}} for row in self.sinks]).encode()
        if argv[0] == 'pw-cli':
            self.mutations.append(argv)
            index = int(argv[argv.index('set-param')+1]); value = float(argv[-1].split(':')[1].rstrip(' }'))
            if self.fail_restore and value > .5: raise OSError('generated transient failure')
            if not self.ignore_setter: self.gains[index] = value
            return b''
        if 'list' in argv:
            return json.dumps(self.sinks if argv[-1] == 'sinks' else self.streams).encode()
        self.mutations.append(argv)
        rows = self.sinks if 'set-sink-volume' in argv else self.streams
        index = argv.index('set-sink-volume' if 'set-sink-volume' in argv else 'set-sink-input-volume')
        target = next(row for row in rows if row['index'] == int(argv[index + 1]))
        values = list(map(int, argv[index + 2:]))
        if self.fail_restore and values[0] > 20000:
            raise OSError('generated transient failure')
        for item, value in zip(target['volume'].values(), values):
            item['value'] = value
        return b''


class OutputPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.pulse = Pulse()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / 'outputs.json'
        self.ducker = PlaybackDucker(runner=self.pulse, server='unix:/generated/pulse/native', journal=self.journal, pipewire_remote='/generated/pipewire-0')

    def volumes(self):
        return [round(self.pulse.gains[self.pulse.sinks[0]['index']], 6)]

    def test_late_notification_and_media_share_gain_without_changing_stream_volumes(self):
        before = copy.deepcopy(self.pulse.streams)
        hardware = copy.deepcopy(self.pulse.sinks)
        self.ducker.start()
        self.assertEqual(self.volumes(), [.25])
        self.assertEqual(self.pulse.streams, before)
        self.assertEqual(self.pulse.sinks, hardware)
        self.pulse.streams.append({'index': 8, 'volume': {'mono': {'value': 65536}}, 'properties': {}})
        # New sounds already route through the lowered output; no stream poll.
        self.assertEqual(self.volumes(), [.25])
        self.ducker.stop()
        self.assertEqual(self.volumes(), [1.0])
        self.assertTrue(all(cmd[0] == 'pw-cli' and 'volume' in cmd[-1] for cmd in self.pulse.mutations))

    def test_repeated_start_never_compounds_and_each_take_restores(self):
        for _ in range(3):
            self.ducker.start(); self.ducker.start()
            self.assertEqual(self.volumes(), [.25])
            self.ducker.stop(); self.ducker.stop()
            self.assertEqual(self.volumes(), [1.0])

    def test_restart_restores_exact_owned_output_values(self):
        self.ducker.start()
        resumed = PlaybackDucker(runner=self.pulse, server=self.ducker.server, journal=self.journal, pipewire_remote='/generated/pipewire-0')
        resumed.stop()
        self.assertEqual(self.volumes(), [1.0])

    def test_manual_output_change_wins(self):
        self.ducker.start()
        self.pulse.gains[1] = .5
        self.ducker.stop()
        self.assertEqual(self.volumes(), [.5])

    def test_new_output_is_lowered_once_and_restored(self):
        self.ducker.start()
        second = copy.deepcopy(self.pulse.sinks[0])
        second.update(index=2, name='generated-second')
        second['properties']['device.serial'] = 'generated-second'
        for item in second['volume'].values(): item['value'] = 32000
        self.pulse.sinks.append(second); self.pulse.gains[2] = .8
        self.ducker.refresh(); self.ducker.refresh()
        self.assertAlmostEqual(self.pulse.gains[2], .2, places=6)
        self.ducker.stop()
        self.assertAlmostEqual(self.pulse.gains[2], .8, places=6)

    def test_failed_restore_keeps_journal_for_retry(self):
        self.ducker.start(); self.pulse.fail_restore = True
        with self.assertRaises(OSError): self.ducker.stop()
        self.assertTrue(json.loads(self.journal.read_text()))
        self.pulse.fail_restore = False; self.ducker.stop()
        self.assertEqual(self.volumes(), [1.0])

    def test_acknowledged_but_ignored_restore_keeps_recovery(self):
        self.ducker.start(); self.pulse.ignore_setter = True
        with self.assertRaises(OSError): self.ducker.stop()
        self.assertTrue(self.ducker.pending)
        self.pulse.ignore_setter = False; self.ducker.stop()
        self.assertEqual(self.volumes(), [1.0])

    def test_temporarily_unready_output_retries_lowering_after_readback_failure(self):
        self.pulse.ignore_setter = True
        with self.assertRaises(OSError): self.ducker.start()
        self.pulse.ignore_setter = False
        self.ducker.refresh()
        self.assertEqual(self.volumes(), [.25])
        self.ducker.stop()
        self.assertEqual(self.volumes(), [1.0])

    def test_native_float_readback_rounding_does_not_leave_output_quiet(self):
        self.pulse.gains[1] = 7.12345
        self.pulse.round_readback = True
        self.ducker.start(); self.ducker.stop()
        self.assertAlmostEqual(self.pulse.gains[1], 7.12345, places=5)

    def test_reused_index_does_not_restore_unrelated_device(self):
        self.ducker.start()
        self.pulse.sinks[0]['properties']['device.serial'] = 'another-output'
        self.ducker.stop()
        self.assertEqual(self.volumes(), [.25])

    def test_output_returning_at_new_index_recovers_original_gain(self):
        self.ducker.start()
        saved = self.pulse.sinks.pop()
        self.ducker.stop()
        saved['index'] = 99; self.pulse.gains[99] = self.pulse.gains.pop(1); self.pulse.sinks.append(saved)
        self.ducker.stop()
        self.assertEqual(self.volumes(), [1.0])

    def test_legacy_stream_journal_is_restored_before_first_output_duck(self):
        legacy = Path(self.tmp.name) / 'playback.json'
        row = self.pulse.streams[0]
        legacy.write_text(json.dumps([{'index': 7, 'identity': PlaybackDucker._identity(row),
                                      'original': [50000], 'lowered': [12500]}]))
        row['volume']['mono']['value'] = 12500
        ducker = PlaybackDucker(runner=self.pulse, server=self.ducker.server,
                                journal=self.journal, legacy_journal=legacy, pipewire_remote='/generated/pipewire-0')
        ducker.start()
        self.assertEqual(row['volume']['mono']['value'], 50000)
        ducker.stop()
