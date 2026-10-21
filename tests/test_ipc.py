import json
from pathlib import Path
import socket
import tempfile
import unittest
from local_voice.ipc import Instance, AlreadyRunning, send_command


class IpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'runtime'

    def test_single_instance_and_private_safe_status(self):
        calls=[]
        with Instance(self.root) as owner:
            owner.serve(lambda command: calls.append(command) or {'ok':True,'state':'idle'})
            with self.assertRaises(AlreadyRunning):
                Instance(self.root).__enter__()
            result=send_command(self.root,'status')
            self.assertEqual(result,{'ok':True,'state':'idle'})
            self.assertEqual(calls,['status'])
            self.assertEqual((self.root/'control.sock').stat().st_mode & 0o777,0o600)
        self.assertFalse((self.root/'control.sock').exists())

    def test_unknown_command_cannot_reach_handler(self):
        calls=[]
        with Instance(self.root) as owner:
            owner.serve(lambda command:calls.append(command) or {'ok':True})
            result=send_command(self.root,'arbitrary')
            self.assertFalse(result['ok'])
            self.assertEqual(calls,[])

    def test_stale_regular_file_is_never_deleted(self):
        self.root.mkdir()
        path=self.root/'control.sock';path.write_text('generated unrelated file')
        with self.assertRaises(ValueError):
            with Instance(self.root): pass
        self.assertEqual(path.read_text(),'generated unrelated file')

    def test_missing_app_status_has_no_state_creation_side_effect(self):
        self.assertFalse(send_command(self.root,'status')['ok'])
        self.assertFalse(self.root.exists())


if __name__=='__main__':unittest.main()
