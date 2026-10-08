"""CS Browser Service - a real browser (Chromium) for the Odoo AI assistant.

The assistant reads and changes data through the Odoo API; this service lets it see the pages as the
users see them: open a page, read its text and controls, take a screenshot.

Endpoints (all but /health need "Authorization: Bearer <BROWSER_TOKEN>"):
  GET    /health                          status, Chromium version, open sessions
  POST   /v1/sessions                     {"allowed_hosts": [...], "locale"}  ->  {"session_id"}
  POST   /v1/sessions/{id}/open           {"url"}                             ->  the page (as /read)
  GET    /v1/sessions/{id}/read                                               ->  url, title, text, elements
  POST   /v1/sessions/{id}/screenshot     {"full_page"}                       ->  {"png": base64, ...}
  POST   /v1/sessions/{id}/login          {"url", "db", "login", "password"}   ->  the page + logged_in
  POST   /v1/sessions/{id}/act            {"action", "ref", "text", "allow_changes"} -> the page after it
  GET    /v1/sessions/{id}/frame                                              ->  image/jpeg of the screen now
  POST   /v1/sessions/{id}/human          {"action": "click|type|key|scroll", "x", "y", "text", "key"}
                                          the consultant drives the browser himself (e.g. to sign in): his
                                          input never goes to the AI, and changes stay blocked as for the AI
  DELETE /v1/sessions/{id}

The pages show a visible mouse pointer (it moves to each control before the click, and a ring marks the
click) so the live view and the screenshots show what the assistant does.

Changes are blocked unless allowed: on Odoo sites every call that is not a read (save, confirm, delete,
buttons, server actions, messages...) is refused while "allow_changes" is not given for the action
(Odoo gives it once the consultant has approved), and the controls that obviously change data are not
even clicked: the answer says the approval is needed.

A session is an isolated browser (own cookies), closed after BROWSER_IDLE_MINUTES without use.
It only opens the sites of its allowed_hosts (given by Odoo, not by the AI): any other page, link,
redirect or frame is blocked. Nothing is downloaded and nothing is kept on disk.
"""
import asyncio
import base64
import hmac
import ipaddress
import os
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
import json
from typing import Literal
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

TOKEN = os.environ.get('BROWSER_TOKEN', '')
MAX_SESSIONS = int(os.environ.get('BROWSER_MAX_SESSIONS', '6'))
IDLE_SECONDS = int(os.environ.get('BROWSER_IDLE_MINUTES', '15')) * 60
NAV_TIMEOUT = int(os.environ.get('BROWSER_TIMEOUT', '45')) * 1000
MAX_TEXT = int(os.environ.get('BROWSER_MAX_TEXT', '20000'))
MAX_ELEMENTS = 150
# Features of this version, checked by Odoo: 1 open/read/screenshot, 2 login/act, 3 live frame + human control
API_VERSION = 3
SCREEN = {'width': 1440, 'height': 900}
BLOCK_MARK = 'Blocked by the AI assistant'

# Calls of the Odoo web client that only read. Any other method (web_save, write, create, unlink,
# action_*, button_*, message_post...) changes data.
READ_METHODS = {
    'read', 'search', 'search_read', 'search_count', 'read_group', 'name_search', 'name_get', 'fields_get',
    'default_get', 'onchange', 'get_views', 'load_views', 'web_search_read', 'web_read', 'web_read_group',
    'read_progress_bar', 'search_panel_select_range', 'search_panel_select_multi_range', 'has_group',
    'check_access_rights', 'check_access_rule', 'get_formview_id', 'get_formview_action',
    'get_empty_list_help', 'systray_get_activities', 'web_name_search', 'name_search_by_code',
}
READ_PREFIXES = ('get_', 'read_', 'search_', 'retrieve_', 'web_search', 'web_read', 'fetch_', 'check_', 'has_')
# Writes of the web client that only keep a display preference or a "seen" mark: not data of the customer
HARMLESS_METHODS = {'set_res_users_settings', 'channel_fetched', 'set_last_seen_message', 'channel_set_last_seen'}
# Other POST routes of the web client that only read (or log in)
READ_ROUTES = (
    '/web/login', '/web/action/load', '/web/session/get_session_info', '/web/session/check',
    '/web/webclient/', '/web/bundle', '/web/dataset/load', '/mail/data', '/mail/init_messaging', '/mail/action',
    '/mail/thread/data', '/mail/thread/messages', '/mail/load_message_failures', '/mail/inbox/messages',
    '/mail/starred/messages', '/mail/history/messages', '/mail/thread/recipients', '/discuss/channel/messages',
    '/discuss/channel/info', '/discuss/channel/members', '/bus/', '/websocket', '/web/image', '/web/content',
    '/website/translations', '/web/translations', '/im_livechat/init',
)
RPC_ROUTES = ('/web/dataset/call_kw', '/web/dataset/call_button', '/web/dataset/call')
# Controls that change data when clicked: asked for approval before the click
RISKY_WORDS = re.compile(
    r'\b(delete|remove|archive|unarchive|confirm|validate|post|cancel|send|approve|refuse|reject|reset|lock|'
    r'unlock|done|pay|register|save|apply|import|merge|duplicate|reconcile|publish|unpublish|install|'
    r'uninstall|upgrade)\b|حذف|ازالة|إزالة|أرشف|ارشف|تأكيد|تاكيد|اعتماد|ترحيل|إلغاء|الغاء|إرسال|ارسال|حفظ|'
    r'دفع|سداد|موافقة|رفض|تثبيت|استيراد|دمج|نشر|تسوية|إقفال|اقفال', re.I)
KEYS = {'Enter', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'PageDown', 'PageUp',
        'Home', 'End', 'Backspace'}

# A visible mouse pointer: headless Chromium has none. It follows the real mouse events of the page,
# keeps its place across pages, and a ring marks each click.
CURSOR_JS = r"""(() => {
  if (window.__csCursor) return;
  window.__csCursor = true;
  const KEY = '__cs_cursor_pos';
  let pos = {x: 40, y: 40};
  try { pos = JSON.parse(sessionStorage.getItem(KEY)) || pos; } catch (e) {}
  let el = null;
  const draw = () => {
    if (!document.body) return null;
    if (!el || !el.isConnected) {
      el = document.createElement('div');
      el.id = '__cs_cursor';
      el.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M3 2l7 19 2.6-7.6L20 11z" '
        + 'fill="#e8344e" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
      el.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;'
        + 'filter:drop-shadow(0 1px 2px rgba(0,0,0,.45));transition:transform .08s linear;';
      document.body.appendChild(el);
    }
    el.style.transform = 'translate(' + (pos.x - 3) + 'px,' + (pos.y - 2) + 'px)';
    return el;
  };
  addEventListener('mousemove', (e) => {
    pos = {x: e.clientX, y: e.clientY};
    try { sessionStorage.setItem(KEY, JSON.stringify(pos)); } catch (err) {}
    draw();
  }, true);
  addEventListener('mousedown', (e) => {
    if (!document.body) return;
    const ring = document.createElement('div');
    ring.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;width:34px;height:34px;'
      + 'border-radius:50%;border:3px solid #e8344e;background:rgba(232,52,78,.18);left:' + (e.clientX - 17)
      + 'px;top:' + (e.clientY - 17) + 'px;transition:transform .6s ease-out,opacity .6s ease-out;';
    document.body.appendChild(ring);
    requestAnimationFrame(() => { ring.style.transform = 'scale(1.9)'; ring.style.opacity = '0'; });
    setTimeout(() => ring.remove(), 900);
  }, true);
  document.addEventListener('DOMContentLoaded', draw);
  setInterval(draw, 1000);  // pages that replace the whole body
})();"""

DESCRIBE_JS = r"""(el) => {
  const b = el.closest('button, a, [role=button], [role=menuitem]') || el;
  return {
    label: (b.getAttribute('aria-label') || b.innerText || b.getAttribute('title') || b.value || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    tag: b.tagName.toLowerCase(), name: b.getAttribute('name') || '', type: b.getAttribute('type') || '',
    input_type: el.getAttribute('type') || '', css: String(b.className || ''),
  };
}"""

# Visible controls of the page, numbered: the assistant names them by their number (data-cs-ref).
ELEMENTS_JS = r"""(max) => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link],'
            + ' [role=menuitem], [role=tab], [role=checkbox], [role=option], [contenteditable=true]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1;
    el.setAttribute('data-cs-ref', String(n));
    if (out.length >= max) continue;
    const tag = el.tagName.toLowerCase();
    const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const name = (el.getAttribute('aria-label') || label || el.innerText || el.getAttribute('placeholder')
                  || el.getAttribute('title') || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim();
    const item = {ref: n, role: el.getAttribute('role') || (tag === 'a' ? 'link' : tag), name: name.slice(0, 80)};
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      item.type = el.getAttribute('type') || tag;
      if (item.type !== 'password') item.value = String(el.value || '').slice(0, 80);
    }
    if (el.disabled) item.disabled = true;
    out.push(item);
  }
  return {elements: out, total: n};
}"""


def authorized(authorization: str = Header(default='')):
    if not TOKEN:
        raise HTTPException(503, 'BROWSER_TOKEN is not set on the server.')
    sent = authorization[7:] if authorization.lower().startswith('bearer ') else ''
    if not hmac.compare_digest(sent.encode(), TOKEN.encode()):
        raise HTTPException(401, 'Invalid token.')


def _host_allowed(host, allowed):
    """Allowed host or one of its subdomains ("example.com" allows "www.example.com")."""
    host = (host or '').lower().rstrip('.')
    return any(host == a or host.endswith('.' + a) for a in allowed)


_PUBLIC = {}


def _is_public(host):
    """Resources of other sites (CDN fonts, images) are loaded only from public addresses: the browser
    cannot be used to reach the internal network of the server."""
    if host in _PUBLIC:
        return _PUBLIC[host]
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
        public = bool(addresses) and all(ipaddress.ip_address(a.split('%')[0]).is_global for a in addresses)
    except (OSError, ValueError):
        public = False
    _PUBLIC[host] = public
    return public


class Session:
    def __init__(self, context, allowed):
        self.id = uuid.uuid4().hex
        self.context = context
        self.allowed = allowed
        self.page = None
        self.blocked = []
        self.allow_changes = False
        self.blocked_changes = []
        self.applied_changes = []
        self.lock = asyncio.Lock()
        self.used = time.monotonic()
        self.frame = None
        self.frame_live = False

    def _change(self, request, path):
        """The change a POST request makes (None for a read)."""
        if request.method != 'POST':
            return None
        if path.startswith(RPC_ROUTES):
            try:
                params = json.loads(request.post_data or '{}').get('params') or {}
            except (ValueError, AttributeError):
                params = {}
            method = params.get('method') or path.rstrip('/').split('/')[-1]
            if method in READ_METHODS or method in HARMLESS_METHODS or method.startswith(READ_PREFIXES):
                return None
            return '%s.%s' % (params.get('model') or '?', method)
        if path.startswith(READ_ROUTES):
            return None
        return 'POST %s' % path

    async def guard(self, route, request):
        url = request.url
        parsed = urlparse(url)
        if parsed.scheme in ('data', 'blob', 'about'):
            return await route.continue_()
        if parsed.scheme in ('http', 'https'):
            if _host_allowed(parsed.hostname, self.allowed):
                change = self._change(request, parsed.path)
                if change and not self.allow_changes:
                    self.blocked_changes.append(change)
                    if parsed.path.startswith(RPC_ROUTES):  # the Odoo client shows it as a normal error
                        return await route.fulfill(status=200, content_type='application/json', body=json.dumps({
                            'jsonrpc': '2.0', 'id': None, 'error': {'code': 200, 'message': BLOCK_MARK, 'data': {
                                'name': 'odoo.exceptions.UserError', 'debug': '', 'arguments': [], 'context': {},
                                'message': '%s: changes need the approval of the consultant.' % BLOCK_MARK}}}))
                    return await route.abort('blockedbyclient')
                if change:
                    self.applied_changes.append(change)
                if request.is_navigation_request():
                    # Redirects are not intercepted by the browser: the page is fetched without following
                    # them, so each step of a redirect comes back here and is checked like a link.
                    try:
                        response = await route.fetch(max_redirects=0)
                    except PlaywrightError:
                        return await route.abort('failed')
                    return await route.fulfill(response=response)
                return await route.continue_()
            if not request.is_navigation_request() and await asyncio.to_thread(_is_public, parsed.hostname):
                return await route.continue_()
        if request.is_navigation_request():
            self.blocked.append(url[:300])
        await route.abort('blockedbyclient')

    def on_page(self, page):
        self.page = page  # a link opened in a new tab: the assistant follows it
        if getattr(page, '_cs_ready', False):
            return
        page._cs_ready = True
        page.on('dialog', lambda dialog: asyncio.ensure_future(_dismiss(dialog)))
        page.set_default_timeout(NAV_TIMEOUT)
        asyncio.ensure_future(self._screencast(page))

    async def _screencast(self, page):
        """Chromium pushes a frame each time the screen changes: the live view reads the latest one at
        once, instead of a screenshot per request (slow, and stuck while a page loads)."""
        try:
            cdp = await page.context.new_cdp_session(page)
        except PlaywrightError:
            return

        def on_frame(params):
            if page is self.page:
                self.frame = base64.b64decode(params['data'])
                self.frame_live = True
            asyncio.ensure_future(_ack(cdp, params['sessionId']))

        cdp.on('Page.screencastFrame', on_frame)
        try:
            await cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': 55, 'everyNthFrame': 1,
                                                    'maxWidth': SCREEN['width'], 'maxHeight': SCREEN['height']})
        except PlaywrightError:
            pass


async def _ack(cdp, frame_session):
    try:
        await cdp.send('Page.screencastFrameAck', {'sessionId': frame_session})
    except PlaywrightError:
        pass


async def _dismiss(dialog):
    try:
        if dialog.type == 'beforeunload':
            await dialog.accept()  # leaving a page with unsaved changes: they are not saved
        else:
            await dialog.dismiss()  # alerts and confirmations would block the page
    except PlaywrightError:
        pass


STATE = {'sessions': {}}


async def _reaper():
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for sid, session in list(STATE['sessions'].items()):
            if now - session.used > IDLE_SECONDS and not session.lock.locked():
                await _close(sid)


async def _close(sid):
    session = STATE['sessions'].pop(sid, None)
    if session:
        try:
            await session.context.close()
        except PlaywrightError:
            pass


@asynccontextmanager
async def lifespan(_app):
    playwright = await async_playwright().start()
    STATE['browser'] = await playwright.chromium.launch(args=['--disable-dev-shm-usage'])
    reaper = asyncio.create_task(_reaper())
    yield
    reaper.cancel()
    for sid in list(STATE['sessions']):
        await _close(sid)
    await STATE['browser'].close()
    await playwright.stop()


app = FastAPI(title='CS Browser Service', version='1.0', docs_url=None, redoc_url=None, lifespan=lifespan)


class NewSession(BaseModel):
    allowed_hosts: list[str] = Field(min_length=1)
    locale: str = 'ar-SA'


class OpenPage(BaseModel):
    url: str


class Screenshot(BaseModel):
    full_page: bool = False


class Login(BaseModel):
    url: str
    db: str = ''
    login: str
    password: str


class Human(BaseModel):
    action: Literal['click', 'type', 'key', 'scroll']
    x: float = 0
    y: float = 0
    text: str = Field('', max_length=500)
    key: str = ''


HUMAN_KEYS = KEYS | {'Delete', 'Space'}


class Act(BaseModel):
    action: Literal['click', 'type', 'select', 'press']
    ref: int | None = None
    text: str = ''
    submit: bool = False
    allow_changes: bool = False


def _session(sid):
    session = STATE['sessions'].get(sid)
    if not session:
        raise HTTPException(404, 'Browser session expired.')
    session.used = time.monotonic()
    return session


async def _settle(page):
    """Wait for the page to be drawn (Odoo builds its screens after the load)."""
    try:
        await page.wait_for_load_state('networkidle', timeout=8000)
    except PlaywrightTimeout:
        pass
    try:  # Odoo: the loading indicator
        await page.wait_for_selector('.o_loading_indicator, .o_blockUI', state='detached', timeout=5000)
    except PlaywrightTimeout:
        pass


async def _check_site(session):
    """Last guard: a page of another site is never read nor photographed."""
    page = session.page
    if not page or page.is_closed():
        raise HTTPException(409, 'No page is open: open one first.')
    host = urlparse(page.url).hostname
    if page.url != 'about:blank' and not _host_allowed(host, session.allowed):
        session.blocked.append(page.url[:300])
        await page.goto('about:blank')
    return page


async def _read(session):
    page = await _check_site(session)
    try:
        text = await page.evaluate('() => document.body ? document.body.innerText : ""')
        controls = await page.evaluate(ELEMENTS_JS, MAX_ELEMENTS)
        title = await page.title()
    except PlaywrightError as e:
        raise HTTPException(422, 'The page could not be read: %s' % str(e)[:300]) from e
    text = re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t ]+', ' ', text or '')).strip()
    result = {
        'url': page.url, 'title': title,
        'text': text[:MAX_TEXT], 'text_truncated': len(text) > MAX_TEXT,
        'elements': controls['elements'], 'elements_total': controls['total'],
    }
    if session.blocked:
        result['blocked'] = session.blocked[-5:]
        session.blocked.clear()
    if session.blocked_changes:
        result['blocked_changes'] = sorted(set(session.blocked_changes))
        session.blocked_changes.clear()
    if session.applied_changes:
        result['applied_changes'] = session.applied_changes[:20]
        session.applied_changes.clear()
    return result


@app.get('/health')
async def health():
    browser = STATE.get('browser')
    status = 'ok' if browser and browser.is_connected() else 'degraded'
    if not TOKEN:
        status = 'token missing: set BROWSER_TOKEN in the stack environment variables'
    return {'status': status, 'api_version': API_VERSION, 'chromium': browser.version if browser else '',
            'sessions': len(STATE['sessions']), 'max_sessions': MAX_SESSIONS}


@app.post('/v1/sessions', dependencies=[Depends(authorized)])
async def new_session(body: NewSession):
    allowed = sorted({h.strip().lower().rstrip('.') for h in body.allowed_hosts if h and h.strip()})
    if not allowed:
        raise HTTPException(400, 'allowed_hosts is empty.')
    if len(STATE['sessions']) >= MAX_SESSIONS:  # the least recently used one makes room
        oldest = min(STATE['sessions'].values(), key=lambda s: s.used)
        await _close(oldest.id)
    context = await STATE['browser'].new_context(
        viewport=SCREEN, locale=body.locale, accept_downloads=False, ignore_https_errors=False,
        service_workers='block')
    session = Session(context, allowed)
    await context.route('**/*', session.guard)
    await context.add_init_script(CURSOR_JS)
    context.on('page', session.on_page)
    session.on_page(await context.new_page())
    STATE['sessions'][session.id] = session
    return {'session_id': session.id, 'allowed_hosts': allowed}


@app.post('/v1/sessions/{sid}/open', dependencies=[Depends(authorized)])
async def open_page(sid: str, body: OpenPage):
    session = _session(sid)
    parsed = urlparse(body.url)
    if parsed.scheme not in ('http', 'https') or not _host_allowed(parsed.hostname, session.allowed):
        raise HTTPException(403, 'This site is not allowed: %s' % (parsed.hostname or body.url))
    async with session.lock:
        status = None
        try:
            if session.page.url.split('#')[0] == body.url.split('#')[0]:
                await session.page.goto('about:blank')  # same page: loaded again, not only its #anchor
            response = await session.page.goto(body.url, wait_until='domcontentloaded')
            status = response.status if response else None
        except PlaywrightTimeout:
            pass  # slow page: read what is there
        except PlaywrightError as e:
            if not session.blocked:
                raise HTTPException(502, 'The page could not be opened: %s' % str(e).splitlines()[0][:300]) from e
        await _settle(session.page)
        result = await _read(session)
    result['http_status'] = status
    return result


@app.get('/v1/sessions/{sid}/read', dependencies=[Depends(authorized)])
async def read_page(sid: str):
    session = _session(sid)
    async with session.lock:
        await _settle(session.page)
        return await _read(session)


@app.post('/v1/sessions/{sid}/screenshot', dependencies=[Depends(authorized)])
async def screenshot(sid: str, body: Screenshot):
    session = _session(sid)
    async with session.lock:
        await _settle(session.page)
        page = await _check_site(session)
        if page.url == 'about:blank':
            raise HTTPException(409, 'No page is open: open one first.')
        try:
            png = await page.screenshot(full_page=body.full_page, animations='disabled', timeout=NAV_TIMEOUT)
        except PlaywrightError as e:
            raise HTTPException(422, 'Screenshot failed: %s' % str(e)[:300]) from e
        return {'url': page.url, 'title': await page.title(), 'png': base64.b64encode(png).decode()}


async def _close_block_dialogs(page):
    """The error dialog of a blocked change is closed: the page stays usable (the read says what was blocked)."""
    dialogs = page.locator('.modal:has-text("%s")' % BLOCK_MARK)
    for _i in range(3):
        if not await dialogs.count():
            return
        try:
            await dialogs.first.locator('.btn-close, .modal-footer .btn').first.click(timeout=3000)
        except PlaywrightError:
            return


@app.post('/v1/sessions/{sid}/login', dependencies=[Depends(authorized)])
async def login(sid: str, body: Login):
    """Sign in to an Odoo database with the credentials Odoo sends (never shown to the AI)."""
    session = _session(sid)
    parsed = urlparse(body.url)
    if parsed.scheme not in ('http', 'https') or not _host_allowed(parsed.hostname, session.allowed):
        raise HTTPException(403, 'This site is not allowed: %s' % (parsed.hostname or body.url))
    target = (parsed.path or '/odoo') + ('?' + parsed.query if parsed.query else '')
    if target.startswith('/web/login'):
        target = '/web'
    query = {'redirect': target}
    if body.db:
        query['db'] = body.db
    async with session.lock:
        page = session.page
        try:
            await page.goto('%s://%s/web/login?%s' % (parsed.scheme, parsed.netloc, urlencode(query)),
                            wait_until='domcontentloaded')
            form = page.locator('form.oe_login_form, form[action*="/web/login"]').first
            await form.locator('input[name=login]').fill(body.login)
            await form.locator('input[name=password]').fill(body.password)
            await form.locator('button[type=submit]').first.click()
            await page.wait_for_load_state('domcontentloaded')
        except PlaywrightError as e:
            raise HTTPException(502, 'Login failed: %s' % str(e).splitlines()[0][:300]) from e
        await _settle(page)
        result = await _read(session)
    result['logged_in'] = '/web/login' not in page.url
    if not result['logged_in']:
        error = page.locator('.alert-danger')
        result['login_error'] = (await error.first.inner_text()).strip()[:200] if await error.count() else ''
    return result


@app.post('/v1/sessions/{sid}/act', dependencies=[Depends(authorized)])
async def act(sid: str, body: Act):
    """Click, type, choose an option or press a key, then read the page."""
    session = _session(sid)
    async with session.lock:
        page = await _check_site(session)
        target = None
        if body.action != 'press':
            if not body.ref:
                raise HTTPException(400, 'ref is required: the number of the control in the last read.')
            target = page.locator('[data-cs-ref="%d"]' % body.ref).first
            if not await target.count():
                raise HTTPException(404, 'Control %s is not on the page anymore: read the page again.' % body.ref)
            control = await target.evaluate(DESCRIBE_JS)
            if body.action == 'type' and control['input_type'] == 'password':
                raise HTTPException(403, 'The assistant never types passwords.')
            risky = (control['type'] == 'object' and control['name']) or 'o_form_button_save' in control['css'] \
                or RISKY_WORDS.search('%s %s' % (control['label'], control['name']))
            if body.action == 'click' and risky and not body.allow_changes:
                return {'needs_approval': True, 'control': control, 'url': page.url, 'title': await page.title()}
        elif body.text not in KEYS:
            raise HTTPException(400, 'Key not allowed: %s (allowed: %s)' % (body.text, ', '.join(sorted(KEYS))))
        session.allow_changes = body.allow_changes
        try:
            if target is not None:
                await _move_to(page, target)
            if body.action == 'click':
                await target.click(timeout=15000)
            elif body.action == 'type':
                if len(body.text) <= 80:  # typed as a user does: visible in the live view
                    await target.fill('', timeout=15000)
                    await target.press_sequentially(body.text, delay=35, timeout=15000)
                else:
                    await target.fill(body.text, timeout=15000)
                if body.submit:
                    await target.press('Enter')
            elif body.action == 'select':
                try:
                    await target.select_option(label=body.text, timeout=5000)
                except PlaywrightError:
                    await target.select_option(value=body.text, timeout=5000)
            else:
                await page.keyboard.press(body.text)
            await _settle(page)
        except PlaywrightError as e:
            raise HTTPException(422, 'The action failed: %s' % str(e).splitlines()[0][:300]) from e
        finally:
            session.allow_changes = False
        if session.blocked_changes:
            await _close_block_dialogs(page)
        return await _read(session)


async def _move_to(page, target):
    """Move the visible pointer to the control, as a hand would, before acting on it."""
    try:
        await target.scroll_into_view_if_needed(timeout=5000)
        box = await target.bounding_box()
        if box:
            await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2, steps=18)
            await page.wait_for_timeout(120)
    except PlaywrightError:
        pass  # the action itself reports a real problem


@app.post('/v1/sessions/{sid}/human', dependencies=[Depends(authorized)])
async def human(sid: str, body: Human):
    """Input of the consultant in the live view (click on the picture, typing): e.g. he signs in himself.
    It may type a password (it goes straight to the page); it is never sent back."""
    session = _session(sid)
    async with session.lock:
        page = session.page
        if not page or page.is_closed():
            raise HTTPException(409, 'No page is open.')
        x = min(max(body.x, 0), SCREEN['width'] - 1)
        y = min(max(body.y, 0), SCREEN['height'] - 1)
        try:
            if body.action == 'click':
                await page.mouse.move(x, y, steps=8)
                await page.mouse.click(x, y)
            elif body.action == 'type':
                # The text replaces the content of the field clicked (e.g. a login already filled in)
                await page.evaluate("() => { const el = document.activeElement;"
                                    " if (el && el.select && /^(INPUT|TEXTAREA)$/.test(el.tagName)) el.select(); }")
                await page.keyboard.type(body.text, delay=15)
            elif body.action == 'key':
                if body.key not in HUMAN_KEYS:
                    raise HTTPException(400, 'Key not allowed: %s' % body.key)
                await page.keyboard.press(' ' if body.key == 'Space' else body.key)
            else:
                await page.mouse.wheel(0, max(min(body.y, 2000), -2000))
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=4000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(250)
        except PlaywrightError as e:
            raise HTTPException(422, 'The action failed: %s' % str(e).splitlines()[0][:300]) from e
        if session.blocked_changes:
            await _close_block_dialogs(page)
        session.blocked_changes.clear()
        return {'url': page.url, 'title': await page.title()}


@app.get('/v1/sessions/{sid}/frame', dependencies=[Depends(authorized)])
async def frame(sid: str):
    """What the browser shows now (live view): taken on demand, the last one when the page is busy."""
    session = STATE['sessions'].get(sid)
    if not session:
        raise HTTPException(404, 'Browser session expired.')
    page = session.page
    shown = page and not page.is_closed() and page.url != 'about:blank' \
        and _host_allowed(urlparse(page.url).hostname, session.allowed)
    if not shown:
        return Response(status_code=204)
    if not session.frame_live:  # no frame pushed yet (just opened): one screenshot
        try:
            session.frame = await page.screenshot(type='jpeg', quality=55, timeout=4000, animations='allow')
        except PlaywrightError:
            pass
    if not session.frame:
        return Response(status_code=204)
    return Response(session.frame, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.delete('/v1/sessions/{sid}', dependencies=[Depends(authorized)])
async def close_session(sid: str):
    await _close(sid)
    return {'closed': True}
