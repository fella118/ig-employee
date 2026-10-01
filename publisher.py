"""Publish due Instagram posts from queue.json using the official Instagram Graph API.
Runs in GitHub Actions every 15 minutes (see .github/workflows/publish.yml).

Env:
  IG_ACCESS_TOKEN  (GitHub secret, set by the account owner; never committed)
  IG_USERNAME      Instagram username to publish to (default: sogixel)
  IG_USER_ID       optional; skips the lookup through the linked Facebook Page
  MEDIA_BASE       optional public base URL for media; default is the repo's GitHub Pages URL
  GITHUB_REPOSITORY, GITHUB_TOKEN, GITHUB_STEP_SUMMARY  (provided by GitHub Actions)
"""
import json, os, sys, time, datetime as dt, urllib.request, urllib.parse, urllib.error, pathlib
import tg

ROOT = pathlib.Path(__file__).resolve().parent
QUEUE = ROOT / 'queue.json'
LOG = ROOT / 'log.md'
API = 'https://graph.facebook.com'
TOKEN = os.environ.get('IG_ACCESS_TOKEN', '')
MAX_PER_RUN = 3          # never burst: at most 3 posts per 15-minute run
MAX_ATTEMPTS = 3


def now():
    return dt.datetime.now(dt.timezone.utc)


def call(method, path, params=None):
    params = dict(params or {}, access_token=TOKEN)
    body = urllib.parse.urlencode(params)
    if method == 'GET':
        req = urllib.request.Request(f'{API}/{path}?{body}')
    else:
        req = urllib.request.Request(f'{API}/{path}', data=body.encode(), method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{method} {path}: {e.code} {e.read().decode()[:500]}')


def ig_user_id():
    if os.environ.get('IG_USER_ID'):
        return os.environ['IG_USER_ID']
    want = os.environ.get('IG_USERNAME', 'sogixel').lower()
    pages = call('GET', 'me/accounts', {'fields': 'name,instagram_business_account{id,username}'})
    for p in pages.get('data', []):
        iba = p.get('instagram_business_account')
        if iba and iba.get('username', '').lower() == want:
            return iba['id']
    raise RuntimeError(f'No Instagram professional account @{want} found on the Pages this token can see.')


def media_base():
    if os.environ.get('MEDIA_BASE'):
        return os.environ['MEDIA_BASE'].rstrip('/') + '/'
    owner, repo = os.environ['GITHUB_REPOSITORY'].split('/')
    return f'https://{owner.lower()}.github.io/{repo}/'


def reachable(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def wait_ready(container, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call('GET', container, {'fields': 'status_code,status'})
        code = st.get('status_code')
        if code == 'FINISHED':
            return
        if code in ('ERROR', 'EXPIRED'):
            raise RuntimeError(f'container {container} {code}: {st.get("status")}')
        time.sleep(10)
    raise RuntimeError(f'container {container} not ready after {timeout}s')


def publish(ig, post, base):
    ai = 'true' if post.get('ai_label', True) else 'false'
    url = lambda f: base + f
    for f in post['files'] + ([post['cover']] if post.get('cover') else []):
        if not reachable(url(f)):
            raise LookupError(f'media not online yet: {url(f)}')
    if post['type'] == 'reel':
        p = {'media_type': 'REELS', 'video_url': url(post['files'][0]), 'caption': post['caption'],
             'share_to_feed': 'true', 'is_ai_generated': ai}
        if post.get('cover'):
            p['cover_url'] = url(post['cover'])
        if post.get('trial'):              # trial reel: shown to non-followers first (MANUAL or SS_PERFORMANCE)
            p['trial_params'] = json.dumps({'graduation_strategy': post['trial']})
        c = call('POST', f'{ig}/media', p)['id']
        wait_ready(c)
    elif post['type'] == 'carousel':
        kids = [call('POST', f'{ig}/media', {'image_url': url(f), 'is_carousel_item': 'true'})['id'] for f in post['files']]
        for k in kids:
            wait_ready(k, 300)
        c = call('POST', f'{ig}/media', {'media_type': 'CAROUSEL', 'children': ','.join(kids),
                                        'caption': post['caption'], 'is_ai_generated': ai})['id']
        wait_ready(c, 300)
    elif post['type'] == 'image':
        c = call('POST', f'{ig}/media', {'image_url': url(post['files'][0]), 'caption': post['caption'], 'is_ai_generated': ai})['id']
        wait_ready(c, 300)
    else:
        raise ValueError(f'unknown type {post["type"]}')
    m = call('POST', f'{ig}/media_publish', {'creation_id': c})['id']
    try:
        link = call('GET', m, {'fields': 'permalink'}).get('permalink', '')
    except Exception:
        link = ''
    return m, link


def story(ig, post, base):
    """Repost a published post to Stories as an image (reel cover or first slide). The API has no
    'share post to story' sticker, so the story is a plain image; it expires after 24 h."""
    img = post.get('cover') or (post['files'][0] if post['type'] != 'reel' else None)
    if not img:
        return None
    c = call('POST', f'{ig}/media', {'media_type': 'STORIES', 'image_url': base + img})['id']
    wait_ready(c, 300)
    return call('POST', f'{ig}/media_publish', {'creation_id': c})['id']


def token_check():
    try:
        d = call('GET', 'debug_token', {'input_token': TOKEN}).get('data', {})
    except Exception as e:
        return f'could not check token: {e}'
    exp = d.get('expires_at') or d.get('data_access_expires_at')
    if not d.get('is_valid', False):
        return 'TOKEN INVALID: generate a new long-lived token and update the IG_ACCESS_TOKEN secret.'
    if exp and exp > 0:
        days = (dt.datetime.fromtimestamp(exp, dt.timezone.utc) - now()).days
        if days <= 10:
            return f'Token expires in {days} days: generate a new long-lived token and update the IG_ACCESS_TOKEN secret.'
    return ''


def open_issue(title, body):
    """Open a GitHub issue unless one with the same title is already open. True when created."""
    tok, repo = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if not (tok and repo):
        return False
    h = {'Authorization': f'Bearer {tok}', 'Accept': 'application/vnd.github+json'}
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f'https://api.github.com/repos/{repo}/issues?state=open&per_page=100', headers=h), timeout=30) as r:
            if any(i['title'] == title for i in json.load(r)):
                return False
        urllib.request.urlopen(urllib.request.Request(f'https://api.github.com/repos/{repo}/issues', method='POST',
                               data=json.dumps({'title': title, 'body': body}).encode(), headers=h), timeout=30)
        return True
    except Exception as e:
        print('issue not created:', e)
        return False


def main():
    q = json.loads(QUEUE.read_text(encoding='utf-8'))
    ts = tg.load()
    cid = tg.chat_id(ts)
    due = [p for p in q['posts'] if p['status'] in ('scheduled', 'retry')
           and dt.datetime.fromisoformat(p['publish_at']) <= now()
           and (not ts['require_approval'] or p.get('approval') == 'approved')]
    due.sort(key=lambda p: p['publish_at'])
    summary = []
    if not TOKEN:
        print('IG_ACCESS_TOKEN is not set; nothing published.')
        return
    if ts['paused']:
        print('paused from Telegram (/resume to restart)')
        due = []
    warn = token_check()
    if warn:
        print('::warning::' + warn)
        summary.append(warn)
        if ('INVALID' in warn or 'expires in' in warn) and open_issue('Instagram token needs renewing', warn):
            tg.notify('🔑 ' + warn)
    if due:
        ig = ig_user_id()
        base = media_base()
        for p in due[:MAX_PER_RUN]:
            try:
                mid, link = publish(ig, p, base)
                p.update(status='published', published_at=now().isoformat(timespec='seconds'), media_id=mid, permalink=link)
                line = f'- {p["published_at"]} PUBLISHED {p["id"]} ({p["type"]}{", trial" if p.get("trial") else ""}) {link}'
                if p.get('story'):
                    try:
                        p['story_id'] = story(ig, p, base)
                        line += ' + story'
                    except Exception as e:     # a failed story never fails the post itself
                        p['story_error'] = str(e)[:300]
                        line += f' (story failed: {str(e)[:120]})'
                tg.refresh(p, cid)
                tg.notify(f'📤 Posted {p["id"]}{" + story" if p.get("story_id") else ""}\n{link}')
            except LookupError as e:          # Pages not deployed yet: try again next run, no attempt counted
                line = f'- {now().isoformat(timespec="seconds")} WAITING {p["id"]}: {e}'
            except Exception as e:
                p['attempts'] = p.get('attempts', 0) + 1
                p['last_error'] = str(e)[:800]
                p['status'] = 'failed' if p['attempts'] >= MAX_ATTEMPTS else 'retry'
                line = f'- {now().isoformat(timespec="seconds")} ERROR {p["id"]} attempt {p["attempts"]}: {str(e)[:300]}'
                if p['status'] == 'failed':
                    open_issue(f'Post {p["id"]} failed', p['last_error'])
                    tg.refresh(p, cid)
                    tg.notify(f'⚠️ {p["id"]} failed after {MAX_ATTEMPTS} tries. Tell Claude in the app, it will check the error.')
            print(line)
            summary.append(line)
            with LOG.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
    else:
        print('nothing due')
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY') and summary:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('\n'.join(summary) + '\n')


if __name__ == '__main__':
    main()
