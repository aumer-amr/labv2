"""Reject incomplete, oversized, and unauthenticated WebSocket evidence."""
import base64
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'kubernetes/apps/ai/zeroclaw/workflow'))
import session


def frame(value, opcode=1):
    data = json.dumps(value).encode()
    return bytes([0x80 | opcode, len(data)]) + data if len(data) < 126 else bytes([0x80 | opcode, 126]) + struct.pack('!H', len(data)) + data


class Socket:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def makefile(self, *args):
        return io.BytesIO(self.data)
    def settimeout(self, value):
        pass
    def sendall(self, data):
        pass


class SessionTests(unittest.TestCase):
    def turn(self, frames, valid_nonce=True):
        key = base64.b64encode(b'x' * 16).decode()
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if not valid_nonce:
            accept = 'invalid'
        header = f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n'.encode()
        with patch.object(session.os, 'urandom', side_effect=lambda n: b'x' * n), patch.object(session.socket, 'create_connection', return_value=Socket(header + frames)):
            return session.turn('http://127.0.0.1:42617', 'session', 'Inspect', 5)

    def test_complete_stream_retains_arguments_and_results(self):
        value = self.turn(frame({'type': 'tool_call', 'id': 'a', 'name': 'monitor__call_tool', 'args': {'query': 'up'}}) + frame({'type': 'tool_result', 'id': 'a', 'name': 'monitor__call_tool', 'output': '{"result":1}'}) + frame({'type': 'done', 'full_response': 'done'}))
        self.assertEqual(value['response'], 'done')
        self.assertEqual(value['messages'][0]['tool_calls'][0]['function']['arguments'], {'query': 'up'})
        self.assertEqual(value['messages'][1]['content'], '{"result":1}')

    def test_incomplete_and_unmatched_calls_never_produce_evidence(self):
        for frames in [frame({'type': 'tool_call', 'id': 'a', 'name': 'tool', 'args': {}}) + frame({'type': 'done', 'full_response': 'done'}), frame({'type': 'tool_result', 'id': 'a', 'name': 'tool', 'output': 'ok'})]:
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                self.turn(frames)

    def test_failed_upgrade_and_oversized_or_fragmented_frames_fail_closed(self):
        with self.assertRaises(ValueError):
            self.turn(b'', valid_nonce=False)
        for data in [b'\x81\x7f' + struct.pack('!Q', session.LIMIT + 1), b'\x01\x00', b'\x81\x80']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.turn(data)
        with self.assertRaises(EOFError):
            self.turn(b'')

    def test_non_loopback_runtime_is_rejected(self):
        with self.assertRaises(ValueError):
            session.turn('http://cluster-service:42617', 'session', 'Inspect', 5)
