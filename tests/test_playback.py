import json
import unittest
import tempfile
from pathlib import Path
from local_voice.playback import PlaybackDucker


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.streams = [{'index': 7, 'volume': {'mono': {'value': 50000}},
                         'properties': {'application.name': 'Generated player'}}]
        self.mutations = []
        def run(argv, **kwargs):
            if 'list' in argv: return json.dumps(self.streams).encode()
            self.mutations.append(argv)
            self.streams[0]['volume']['mono']['value'] = int(argv[-1])
            return b''
        self.ducker = PlaybackDucker(runner=run, server='unix:/generated/pulse/native')

    def test_lowers_only_playback_stream_then_restores_exact_volume(self):
        self.ducker.start()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 12500)
        self.ducker.stop()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 50000)
        self.assertTrue(all('set-sink-input-volume' in cmd for cmd in self.mutations))
        self.assertFalse(any('set-sink-volume' in cmd for cmd in self.mutations))

    def test_user_change_wins_and_repeated_stop_is_noop(self):
        self.ducker.start()
        self.streams[0]['volume']['mono']['value'] = 22222
        self.ducker.stop();self.ducker.stop()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 22222)
        self.assertEqual(len(self.mutations), 1)

    def test_repeated_start_does_not_compound_and_new_attempt_lowers_again(self):
        self.ducker.start();self.ducker.start()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 12500)
        self.ducker.stop();self.ducker.start()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 12500)
        self.ducker.stop()

    def test_reused_stream_index_with_other_identity_is_left_alone(self):
        self.ducker.start()
        self.streams[0]['properties']['application.name'] = 'Another generated app'
        self.ducker.stop()
        self.assertEqual(len(self.mutations), 1)

    def test_output_route_change_does_not_leave_owned_stream_quiet(self):
        self.streams[0]['sink'] = 1
        self.ducker.start()
        self.streams[0]['sink'] = 2
        self.ducker.stop()
        self.assertEqual(self.streams[0]['volume']['mono']['value'], 50000)

    def test_explicit_server_must_be_local_unix_without_whitespace(self):
        for value in ['tcp:example:4713', 'unix:/a\ttcp:example:4713', 'unix:/a\u2003b']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PlaybackDucker(server=value)

    def test_restart_restores_journaled_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'playback.json'
            self.ducker.journal = journal
            self.ducker.start()
            replacement = PlaybackDucker(runner=self.ducker.runner, server=self.ducker.server, journal=journal)
            replacement.stop()
            self.assertEqual(self.streams[0]['volume']['mono']['value'], 50000)
            self.assertEqual(json.loads(journal.read_text()), [])


if __name__ == '__main__': unittest.main()
