"""The T3 handoff against a fake T3 server: OAuth sign-in with a `t3 pair` code, MCP over streamable HTTP."""
import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from update_manager import config, handoff, t3mcp


class FakeT3(BaseHTTPRequestHandler):
    """Just enough of T3's OAuth and MCP endpoints. State lives on the server object."""

    def log_message(self, *_):
        pass

    def reply(self, status, body=None, headers=None, sse=False):
        payload = b''
        if body is not None:
            text = json.dumps(body)
            payload = (f'event: message\ndata: {text}\n\n' if sse else text).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/event-stream' if sse else 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
        if self.headers.get('Content-Type', '').startswith('application/x-www-form-urlencoded'):
            return dict(urllib.parse.parse_qsl(raw))
        return json.loads(raw) if raw else {}

    def do_DELETE(self):
        self.reply(200, {})

    def do_POST(self):
        s, body = self.server, self.body()
        if self.path == '/oauth/mcp/register':
            s.registered.append(body)
            return self.reply(201, {'client_id': f'client-{len(s.registered)}'})
        if self.path == '/oauth/mcp/decision':
            if body['decision'] != {'_tag': 'pairing-code', 'access': 'full-access', 'code': 'PAIR-CODE'}:
                return self.reply(400, {'message': 'bad code'})
            s.challenge = body['authorization']['code_challenge']
            return self.reply(200, {'redirectTo': f"{t3mcp.REDIRECT}?code=grant-1&state="})
        if self.path == '/oauth/mcp/token':
            digest = base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).decode()
            if body['code'] != 'grant-1' or digest.rstrip('=') != s.challenge:
                return self.reply(400, {'error': 'invalid_grant'})
            s.sign_ins += 1
            s.valid_token = f'token-{s.sign_ins}'
            return self.reply(200, {'access_token': s.valid_token, 'expires_in': 30 * 86400})
        if self.path == '/mcp':
            if self.headers.get('Authorization') != f'Bearer {s.valid_token}':
                return self.reply(401, {'error': 'invalid_mcp_credential'})
            if body.get('method') == 'initialize':
                return self.reply(200, {'jsonrpc': '2.0', 'id': body['id'], 'result': {'protocolVersion': '2025-06-18'}},
                                  headers={'Mcp-Session-Id': 'session-1'}, sse=True)
            if self.headers.get('Mcp-Session-Id') != 'session-1':
                return self.reply(400, {'error': 'no session'})
            if 'id' not in body:
                return self.reply(202)
            result = self.tool(body['params']['name'], body['params'].get('arguments') or {})
            return self.reply(200, {'jsonrpc': '2.0', 'id': body['id'], 'result': result}, sse=True)
        self.reply(404, {})

    def tool(self, name, args):
        s = self.server
        s.calls.append((name, args))
        if name == 't3_project_list':  # two pages
            page = s.projects[:1] if args.get('cursor', 0) == 0 else s.projects[1:]
            return {'structuredContent': {'projects': page, 'nextCursor': 1 if args.get('cursor', 0) == 0 else None}}
        if name == 't3_project_create':
            project = {'id': 'p-new', 'title': args['title'], 'workspaceRoot': args['workspaceRoot']}
            s.projects.append(project)
            return {'structuredContent': {'project': project}}
        if name == 't3_thread_launch':
            return {'content': [{'type': 'text', 'text': json.dumps({'threadId': 'thread-1', 'projectId': args.get(
                'projectId'), 'runId': 'run-1'})}]}
        return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'message': f'no tool {name}'})}]}


class T3Test(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeT3)
        self.server.registered, self.server.calls, self.server.sign_ins = [], [], 0
        self.server.valid_token, self.server.challenge = None, None
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.server.projects = [{'id': 'p-other', 'title': 'other', 'workspaceRoot': '/elsewhere'},
                                {'id': 'p-repo', 'title': 'repo project', 'workspaceRoot': str(self.repo),
                                 'deletedAt': None}]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.t3 = self.fake_t3(thread_start=False)
        self.settings = config.merge(config.DEFAULTS, {'agent': {'server': self.origin, 't3': [str(self.t3)]}})

    def fake_t3(self, thread_start):
        path = self.root / ('t3-new' if thread_start else 't3')
        start = ('if [ "$1 $2" = "thread --help" ]; then echo "start  Start a thread"; exit 0; fi\n'
                 'if [ "$1 $2" = "thread start" ]; then cat >/dev/null; '
                 'echo \'{"threadId":"cli-thread","projectId":"p-cli"}\'; exit 0; fi\n') if thread_start else (
            'if [ "$1" = thread ]; then echo "Run the T3 Code server."; exit 0; fi\n')
        path.write_text('#!/bin/sh\n' + start + 'if [ "$1" = pair ]; then echo "█▀▀ QR ▀▀█"; '
                        'echo "Token: PAIR-CODE"; exit 0; fi\nexit 1\n')
        path.chmod(0o755)
        return path

    def auth(self, now=time.time):
        return t3mcp.Auth(self.origin, t3=str(self.t3), path=self.root / 'token.json', now=now)

    def test_sign_in_is_cached_private_and_renewed_early(self):
        auth = self.auth()
        self.assertEqual(auth.token(), 'token-1')
        self.assertEqual(auth.token(), 'token-1')  # cached: no second sign-in
        self.assertEqual(self.server.sign_ins, 1)
        self.assertEqual(stat.S_IMODE(os.stat(auth.path).st_mode), 0o600)
        self.assertEqual(self.server.registered[0]['client_name'], f'update-manager on {config.hostname()}')
        later = self.auth(now=lambda: time.time() + 29 * 86400)  # 1 day left: renew
        self.assertEqual(later.token(), 'token-2')

    def test_launch_in_the_existing_project(self):
        with t3mcp.Session(self.origin, self.auth()) as session:
            started = t3mcp.launch(str(self.repo), 'unused', 'Merge upstream into repo', 'the brief',
                                   self.settings, session=session)
        self.assertEqual((started['thread'], started['project'], started['created_project']),
                         ('thread-1', 'repo project', False))
        name, args = self.server.calls[-1]
        self.assertEqual(name, 't3_thread_launch')
        self.assertEqual(args, {'projectId': 'p-repo', 'title': 'Merge upstream into repo', 'message': 'the brief',
                                'workspaceStrategy': {'type': 'root'}, 'runtimeMode': 'full-access',
                                'interactionMode': 'default', 'modelSelection': t3mcp.DEFAULT_MODEL})
        self.assertEqual([c[0] for c in self.server.calls].count('t3_project_list'), 2)  # both pages

    def test_launch_registers_a_missing_project(self):
        other = self.root / 'other-repo'
        other.mkdir()
        with t3mcp.Session(self.origin, self.auth()) as session:
            started = t3mcp.launch(str(other), 'nm-sidebar (nixos-conf vendor)', 'T', 'b', self.settings,
                                   session=session)
        self.assertTrue(started['created_project'])
        self.assertEqual(started['project'], 'nm-sidebar (nixos-conf vendor)')
        self.assertIn(('t3_project_create', {'title': 'nm-sidebar (nixos-conf vendor)',
                                             'workspaceRoot': os.path.realpath(other)}), self.server.calls)

    def test_a_revoked_token_signs_in_again(self):
        auth = self.auth()
        auth.token()
        self.server.valid_token = 'revoked-elsewhere'
        with t3mcp.Session(self.origin, auth) as session:
            self.assertEqual(session.token, 'token-2')

    def test_tool_errors(self):
        with t3mcp.Session(self.origin, self.auth()) as session, self.assertRaisesRegex(t3mcp.T3Error, 'no tool'):
            session.call('t3_nothing')

    def test_event_stream(self):
        text = 'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"ok":1}}\n\n'
        self.assertEqual(t3mcp.parse_message(text, 'text/event-stream', 3)['result'], {'ok': 1})
        with self.assertRaises(t3mcp.T3Error):
            t3mcp.parse_message(text, 'text/event-stream', 4)

    def test_handoff_order(self):
        via_mcp = handoff.launch('repo', str(self.repo), 'brief', 'Merge upstream into repo', 'repo project',
                                 self.settings)
        self.assertEqual(via_mcp['method'], 'mcp')
        self.assertIn('“Merge upstream into repo” in the project “repo project”', via_mcp['message'])
        cli = config.merge(self.settings, {'agent': {'t3': [str(self.fake_t3(thread_start=True))]}})
        self.assertEqual(handoff.launch('repo', str(self.repo), 'brief', 'T', None, cli)['method'], 't3-thread')
        down = config.merge(self.settings, {'agent': {'server': 'http://127.0.0.1:9'}})
        fallback = handoff.launch('repo', str(self.repo), 'brief', 'T', None, down)
        self.assertEqual(fallback['method'], 'brief')
        self.assertTrue(Path(fallback['brief_path']).read_text() == 'brief')
        self.assertIn('T3 MCP', fallback['message'])
