"""T3 Code's MCP endpoint, signed in the way T3 signs in an outside agent.

T3's /mcp takes MCP client tokens only: `t3 auth session issue` bearers are
refused. An outside agent gets one by OAuth with PKCE, approved by a one-time
`t3 pair` code (this machine runs the T3 server, so it can mint one):

1. `t3 pair --ttl 2m --label ... --scope ...` prints `Token: <code>`;
2. POST /oauth/mcp/register registers a client (a redirect URI is required,
   nothing listens on it);
3. POST /oauth/mcp/decision with {_tag: "pairing-code", access: "full-access",
   code} approves it and answers {redirectTo: "<redirect>?code=<grant>"};
4. POST /oauth/mcp/token exchanges the grant (and the PKCE verifier) for an
   access token that lasts 30 days. There is no refresh token, so it is
   renewed 2 days before it expires.

The token is kept 0600 in ~/.local/state/update-manager/t3/. Then MCP over
streamable HTTP: initialize, notifications/initialized, tools/call.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config
from .util import CommandError, read_json, run

REDIRECT = 'http://127.0.0.1:53682/callback'
SCOPES = ('orchestration:read', 'orchestration:operate', 'settings:write', 'source-control:write')
RENEW_S = 2 * 86400
PROTOCOL = '2025-06-18'
DEFAULT_MODEL = {'instanceId': 'claudeAgent', 'model': 'claude-opus-5-5',
                 'options': [{'id': 'effort', 'value': 'xhigh'}, {'id': 'fastMode', 'value': False}]}


class T3Error(RuntimeError):
    pass


class Unauthorized(T3Error):
    pass


def server_origin(settings=None):
    """agent.server, else the running server's own record of where it listens."""
    configured = config.get('agent', 'server', settings=settings)
    if configured:
        return configured.rstrip('/')
    home = Path(os.environ.get('T3CODE_HOME') or Path.home() / '.t3')
    state = read_json(home / 'userdata/server-runtime.json', {}) or {}
    return (state.get('origin') or 'http://127.0.0.1:3773').rstrip('/')


def http(method, url, body=None, headers=None, form=False, timeout=30):
    """(status, headers with lower-case names, text). HTTP errors are answers, not exceptions."""
    data, sent = None, dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            sent['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            data = json.dumps(body).encode()
            sent.setdefault('Content-Type', 'application/json')
    request = urllib.request.Request(url, data=data, headers=sent, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the tailnet is never behind a proxy
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read().decode(errors='replace')
    except (urllib.error.URLError, OSError) as e:
        raise T3Error(f'{url}: {getattr(e, "reason", e)}') from e


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def write_private(path, value):
    """A 0600 file, replaced atomically: it holds a token."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f)
    os.replace(tmp, path)


class Auth:
    """An MCP client token for one T3 server, signed in with a `t3 pair` code when needed."""

    def __init__(self, origin, t3='t3', label=None, path=None, now=time.time):
        self.origin, self.t3, self.now = origin, t3, now
        self.label = label or f'update-manager on {config.hostname()}'
        safe = re.sub(r'[^A-Za-z0-9]+', '-', origin).strip('-')
        self.path = Path(path) if path else config.STATE / 't3' / f'token-{safe}.json'

    def cached(self):
        data = read_json(self.path)
        if data and data.get('origin') == self.origin and data.get('expires_at', 0) > self.now() + RENEW_S:
            return data['token']
        return None

    def token(self, force=False):
        if not force and (token := self.cached()):
            return token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix('.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not force and (token := self.cached()):  # another process signed in while this one waited
                return token
            data = self.sign_in()
            write_private(self.path, data)
            return data['token']

    def forget(self):
        with contextlib.suppress(OSError):
            self.path.unlink()

    def pairing_code(self):
        args = [self.t3, 'pair', '--ttl', '2m', '--label', self.label]
        for scope in SCOPES:
            args += ['--scope', scope]
        try:
            text = run(args, timeout=60)
        except CommandError as e:
            raise T3Error(f't3 pair: {e}') from e
        m = re.search(r'^\s*Token:\s*(\S+)', text, re.M)
        if not m:
            raise T3Error('t3 pair printed no "Token:" line')
        return m[1]

    def sign_in(self):
        code = self.pairing_code()
        status, _, text = http('POST', f'{self.origin}/oauth/mcp/register',
                               {'client_name': self.label, 'redirect_uris': [REDIRECT]})
        if status not in (200, 201):
            raise T3Error(f'registering the client: HTTP {status} {text[:200]}')
        client = json.loads(text)['client_id']
        verifier = b64url(secrets.token_bytes(48))
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        resource = f'{self.origin}/mcp'
        status, _, text = http('POST', f'{self.origin}/oauth/mcp/decision', {
            'authorization': {'response_type': 'code', 'client_id': client, 'redirect_uri': REDIRECT,
                              'code_challenge': challenge, 'code_challenge_method': 'S256', 'resource': resource},
            'decision': {'_tag': 'pairing-code', 'access': 'full-access', 'code': code}})
        redirect = None
        try:
            redirect = json.loads(text).get('redirectTo')
        except (ValueError, AttributeError):
            pass
        if not redirect:  # older servers: find the callback URL anywhere in the answer
            m = re.search(re.escape(REDIRECT) + r'\?[^"\s]*', text)
            redirect = m[0] if m else None
        grant = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect or '').query).get('code', [None])[0]
        if status != 200 or not grant:
            raise T3Error(f'the sign-in was not approved: HTTP {status} {text[:200]}')
        status, _, text = http('POST', f'{self.origin}/oauth/mcp/token', {
            'grant_type': 'authorization_code', 'code': grant, 'redirect_uri': REDIRECT, 'client_id': client,
            'code_verifier': verifier, 'resource': resource}, form=True)
        if status != 200:
            raise T3Error(f'exchanging the grant: HTTP {status} {text[:200]}')
        answer = json.loads(text)
        now = self.now()
        return {'token': answer['access_token'], 'expires_at': now + int(answer.get('expires_in', 30 * 86400)),
                'client_id': client, 'origin': self.origin, 'label': self.label, 'created': now}


def parse_message(text, content_type, ident):
    """The JSON-RPC answer to request ident, from a JSON body or a text/event-stream."""
    if 'text/event-stream' in (content_type or ''):
        for event in re.split(r'\r?\n\r?\n', text):
            data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
            if not data:
                continue
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get('id') == ident:
                return message
        raise T3Error(f'no answer to request {ident} in the event stream')
    return json.loads(text)


class Session:
    """One MCP session: initialize, then tools/call."""

    def __init__(self, origin, auth, client_name='update-manager'):
        self.origin, self.auth, self.client_name = origin, auth, client_name
        self.session_id = None
        self.next_id = 0
        self.token = None

    def post(self, payload):
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json, text/event-stream'}
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
            headers['MCP-Protocol-Version'] = PROTOCOL
        status, response_headers, text = http('POST', f'{self.origin}/mcp', payload, headers, timeout=120)
        if status == 401:
            raise Unauthorized(text[:200])
        if status >= 400:
            raise T3Error(f'MCP {payload.get("method")}: HTTP {status} {text[:300]}')
        self.session_id = response_headers.get('mcp-session-id', self.session_id)
        if 'id' not in payload:
            return None
        message = parse_message(text, response_headers.get('content-type'), payload['id'])
        if message.get('error'):
            raise T3Error(f'MCP {payload.get("method")}: {message["error"].get("message")}')
        return message.get('result')

    def request(self, method, params=None):
        self.next_id += 1
        return self.post({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}})

    def open(self):
        for attempt in range(2):
            self.token = self.auth.token(force=attempt > 0)
            self.session_id = None
            try:
                self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                            'clientInfo': {'name': self.client_name, 'version': __version__}})
                self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                return self
            except Unauthorized:
                if attempt:
                    raise
                self.auth.forget()  # revoked or expired early: sign in again, once
        return self

    def close(self):
        if self.session_id:
            with contextlib.suppress(T3Error):
                http('DELETE', f'{self.origin}/mcp', headers={'Authorization': f'Bearer {self.token}',
                                                             'Mcp-Session-Id': self.session_id}, timeout=10)

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    def call(self, tool, arguments=None):
        result = self.request('tools/call', {'name': tool, 'arguments': arguments or {}}) or {}
        text = next((c.get('text') for c in result.get('content') or [] if c.get('type') == 'text'), '')
        if result.get('isError'):
            try:
                text = json.loads(text).get('message') or text
            except (ValueError, AttributeError):
                pass
            raise T3Error(f'{tool}: {text[:300]}')
        if result.get('structuredContent') is not None:
            return result['structuredContent']
        try:
            return json.loads(text)
        except ValueError:
            return {'text': text}


def connect(settings=None, auth=None):
    origin = server_origin(settings)
    t3 = (config.get('agent', 't3', settings=settings) or ['t3'])[0]
    return Session(origin, auth or Auth(origin, t3=os.path.expanduser(t3)))


def projects(session):
    out, cursor = [], 0
    while cursor is not None:
        page = session.call('t3_project_list', {'limit': 100, 'cursor': cursor})
        out += page.get('projects') or []
        cursor = page.get('nextCursor')
    return out


def same_dir(a, b):
    try:
        return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))
    except (TypeError, ValueError):
        return False


def project_for(session, repo, title):
    """The project whose workspaceRoot is repo, registered as title if there is none. (project, created)."""
    for project in projects(session):
        if not project.get('deletedAt') and same_dir(project.get('workspaceRoot', ''), repo):
            return project, False
    made = session.call('t3_project_create', {'title': title, 'workspaceRoot': os.path.realpath(repo)})
    project = made.get('project') if isinstance(made.get('project'), dict) else made
    ident = project.get('id') or project.get('projectId') or made.get('projectId')
    return {'id': ident, 'title': project.get('title') or title, 'workspaceRoot': os.path.realpath(repo)}, True


def model_selection(settings=None):
    return config.get('agent', 'model_selection', settings=settings) or DEFAULT_MODEL


def launch(repo, project_title, title, message, settings=None, session=None):
    """Start a thread in repo's project (made if missing). Returns {'thread', 'title', 'project', 'projectId',
    'created_project', 'run'}."""
    own = session is None
    session = session or connect(settings).open()
    try:
        project, created = project_for(session, repo, project_title)
        launched = session.call('t3_thread_launch', {
            'projectId': project['id'], 'title': title, 'message': message,
            'workspaceStrategy': {'type': 'root'}, 'runtimeMode': 'full-access', 'interactionMode': 'default',
            'modelSelection': model_selection(settings)})
        return {'thread': launched.get('threadId'), 'title': title, 'project': project.get('title'),
                'projectId': project['id'], 'created_project': created, 'run': launched.get('runId')}
    finally:
        if own:
            session.close()
