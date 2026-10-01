"""Telegram helpers: the owner approves each post from their phone before it goes out.

Env: TELEGRAM_BOT_TOKEN (GitHub secret, set by the account owner; never committed).
State lives in telegram.json. The repo is public, so the paired chat id is stored encrypted
with a key derived from the bot token: without the secret it can't be read.
"""
import json, os, hashlib, pathlib, datetime as dt, urllib.request, urllib.parse, urllib.error

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / 'telegram.json'
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
DEFAULT = {'require_approval': True, 'paused': False, 'chat': None, 'offset': 0}
TZ = dt.timezone(dt.timedelta(hours=1))   # Morocco
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def load():
    s = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    return {**DEFAULT, **s}


def save(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def _key(n):
    k, i = b'', 0
    while len(k) < n:
        k += hashlib.sha256(f'sogixel-tg-chat:{i}:'.encode() + TOKEN.encode()).digest()
        i += 1
    return k[:n]


def seal(chat_id):
    raw = str(chat_id).encode()
    return bytes(a ^ b for a, b in zip(raw, _key(len(raw)))).hex()


def chat_id(s):
    if not (TOKEN and s.get('chat')):
        return None
    raw = bytes.fromhex(s['chat'])
    try:
        return int(bytes(a ^ b for a, b in zip(raw, _key(len(raw)))).decode())
    except ValueError:          # token changed (new bot): pairing must be redone
        return None


def api(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/{method}', data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)['result']
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'telegram {method}: {e.code} {e.read().decode()[:300]}')


def when(p):
    t = dt.datetime.fromisoformat(p['publish_at']).astimezone(TZ)
    return f'{DAYS[t.weekday()]} {t.day} {MONTHS[t.month - 1]} {t:%H:%M}'


def kind(p):
    k = {'reel': 'Reel', 'carousel': 'Carousel', 'image': 'Image'}[p['type']]
    if p.get('trial'):
        k += ' (trial)'
    if p.get('story'):
        k += ' + story'
    return k


def card(p):
    """The approval message: when, what, the exact caption, and where the post stands."""
    a, st = p.get('approval'), p['status']
    if st == 'published':
        line = f'📤 Posted: {p.get("permalink", "")}'
    elif st == 'expired':
        line = '⌛ Not posted: no answer before the slot. Ask Claude to reschedule it.'
    elif st == 'failed':
        line = '⚠️ Approved but publishing failed. Claude has the error.'
    elif a == 'approved':
        line = f'✅ Approved. Goes out {when(p)}. Tap Skip to cancel.'
    elif a == 'skipped':
        line = '❌ Skipped. It will not be posted. Tap Approve to bring it back.'
    else:
        line = '⏳ Waiting for your OK.'
    return f'{when(p)} · {kind(p)}\n{p["id"]}\n\n{p["caption"]}\n\n{line}'


def keyboard(p):
    if p['status'] in ('published', 'expired', 'failed', 'skipped'):
        return None
    return {'inline_keyboard': [[{'text': '✅ Approve', 'callback_data': f'ok:{p["id"]}'},
                                 {'text': '❌ Skip', 'callback_data': f'no:{p["id"]}'}]]}


def refresh(p, cid):
    """Rewrite a post's approval message so it shows the current state."""
    if not (cid and p.get('tg_msg')):
        return
    try:
        api('editMessageText', chat_id=cid, message_id=p['tg_msg'], text=card(p),
            disable_web_page_preview='true', reply_markup=keyboard(p))
    except Exception as e:
        if 'not modified' not in str(e):
            print('telegram edit failed:', e)


def notify(text):
    """Best effort: a Telegram problem never stops publishing."""
    s = load()
    cid = chat_id(s)
    if not cid:
        return
    try:
        api('sendMessage', chat_id=cid, text=text, disable_web_page_preview='true')
    except Exception as e:
        print('telegram notify failed:', e)
