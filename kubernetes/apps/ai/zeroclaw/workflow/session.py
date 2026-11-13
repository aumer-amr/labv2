"""Bounded loopback WebSocket client for ZeroClaw's durable chat endpoint."""
import base64
import hashlib
import json
import os
import socket
import struct
import time
from urllib.parse import urlencode, urlsplit

LIMIT = 2_000_000


def turn(base, session_id, prompt, timeout):
    url = urlsplit(base)
    if url.scheme != 'http' or url.hostname != '127.0.0.1':
        raise ValueError('The monitoring runtime must use loopback')
    deadline = time.monotonic() + timeout
    with socket.create_connection((url.hostname, url.port), timeout=timeout) as sock:
        with sock.makefile('rb') as stream:
            def remaining():
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError('Investigation deadline reached')
                sock.settimeout(seconds)

            def read(size):
                result = bytearray()
                while len(result) < size:
                    remaining()
                    chunk = stream.read1(size - len(result))
                    if not chunk:
                        raise EOFError('ZeroClaw closed the session')
                    result.extend(chunk)
                return bytes(result)

            def send(opcode, data):
                if len(data) > LIMIT:
                    raise ValueError('ZeroClaw request exceeded limit')
                mask = os.urandom(4)
                length = len(data)
                header = bytes([0x80 | opcode])
                header += (bytes([0x80 | length]) if length < 126 else
                           b'\xfe' + struct.pack('!H', length) if length <= 65535 else
                           b'\xff' + struct.pack('!Q', length))
                remaining()
                sock.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))

            key = base64.b64encode(os.urandom(16)).decode()
            query = urlencode({'agent': 'investigator', 'session_id': session_id})
            request = (f'GET /ws/chat?{query} HTTP/1.1\r\nHost: {url.netloc}\r\n'
                       f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                       'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: zeroclaw.v1\r\n\r\n')
            sock.sendall(request.encode())
            headers = bytearray()
            while not headers.endswith(b'\r\n\r\n'):
                if len(headers) >= 16384:
                    raise ValueError('ZeroClaw upgrade headers exceeded limit')
                headers.extend(read(1))
            lines = headers.decode('ascii').split('\r\n')
            fields = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            # Header names are case-insensitive; the nonce value is case-sensitive.
            accept = next((line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('sec-websocket-accept:')), '')
            if lines[0].split()[1] != '101' or fields.get('upgrade', '').strip() != 'websocket' or accept != expected:
                raise ValueError('ZeroClaw rejected the session upgrade')
            send(1, json.dumps({'type': 'message', 'content': prompt}).encode())
            received = 0
            messages = []
            calls = {}
            turn_id = os.urandom(8).hex()
            while True:
                first, second = read(2)
                opcode = first & 0x0f
                # shortcut: v0.9 sends complete frames; reject fragmentation until upstream requires it.
                if first & 0x70 or not first & 0x80 or second & 0x80:
                    raise ValueError('Unsupported ZeroClaw session frame')
                size = second & 0x7f
                if size == 126:
                    size = struct.unpack('!H', read(2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', read(8))[0]
                received += size
                if received > LIMIT or (opcode >= 8 and size > 125):
                    raise ValueError('ZeroClaw response exceeded limit')
                data = read(size)
                if opcode == 9:
                    send(10, data)
                elif opcode == 10:
                    continue
                elif opcode == 1:
                    event = json.loads(data)
                    if event.get('type') == 'error':
                        raise RuntimeError('ZeroClaw investigation failed')
                    if event.get('type') == 'tool_call':
                        call_id = turn_id + event['id']
                        if call_id in calls:
                            raise ValueError('Duplicate ZeroClaw tool call')
                        calls[call_id] = event['name']
                        messages.append({'role': 'assistant', 'tool_calls': [{'id': call_id, 'function': {'name': event['name'], 'arguments': event['args']}}]})
                    elif event.get('type') == 'tool_result':
                        call_id = turn_id + event['id']
                        if calls.pop(call_id, None) != event['name']:
                            raise ValueError('Unmatched ZeroClaw tool result')
                        messages.append({'role': 'tool', 'tool_call_id': call_id, 'tool_name': event['name'], 'content': event['output']})
                    if len(messages) > 500:
                        raise ValueError('ZeroClaw evidence exceeded limit')
                    if event.get('type') == 'done':
                        if calls:
                            raise ValueError('Incomplete ZeroClaw tool evidence')
                        return {'response': event.get('full_response'), 'messages': messages}
                else:
                    raise ValueError('ZeroClaw ended the session before completion')
