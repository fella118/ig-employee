"""Reply to new comments on @sogixel with pre-approved templates (no free-form AI text).

Config: replies.json   ("enabled", "since", templates and keyword lists; edited only with the owner's approval)
State:  replied.json   (comment ids already handled)
Log:    comments_log.md
Needs the instagram_manage_comments permission on the token.
"""
import json, os, re, pathlib, datetime as dt
from publisher import call, ig_user_id, open_issue, TOKEN

ROOT = pathlib.Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'replies.json').read_text(encoding='utf-8'))
STATE = ROOT / 'replied.json'
LOG = ROOT / 'comments_log.md'
ARABIC = re.compile(r'[؀-ۿ]')


def classify(text):
    t = text.lower()
    if 'http' in t or any(w in t for w in CFG['skip_words']):
        return 'skip'
    if any(w in t for w in CFG['interest_words']):
        return 'interest'
    if any(w in t for w in CFG['praise_words']):
        return 'praise'
    return 'default'


def main():
    if not TOKEN or not CFG.get('enabled'):
        print('comment replies disabled')
        return
    since = dt.datetime.fromisoformat(CFG['since'])
    me = os.environ.get('IG_USERNAME', 'sogixel').lower()
    done = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    ig = ig_user_id()
    media = call('GET', f'{ig}/media', {'fields': 'id,timestamp', 'limit': 15}).get('data', [])
    sent, flagged = 0, []
    for m in media:
        comments = call('GET', f'{m["id"]}/comments',
                        {'fields': 'id,text,username,timestamp,replies{username}', 'limit': 50}).get('data', [])
        for c in comments:
            if c['id'] in done or c.get('username', '').lower() == me:
                continue
            ts = dt.datetime.fromisoformat(c['timestamp'].replace('+0000', '+00:00'))
            if ts < since:
                done.add(c['id'])
                continue
            if any(r.get('username', '').lower() == me for r in c.get('replies', {}).get('data', [])):
                done.add(c['id'])          # already answered (by hand or earlier)
                continue
            kind = classify(c.get('text', ''))
            if kind == 'skip':
                flagged.append(f'@{c.get("username")}: {c.get("text", "")[:200]}')
                done.add(c['id'])
                continue
            if sent >= CFG.get('max_per_run', 10):
                break
            lang = 'da' if ARABIC.search(c.get('text', '')) else 'fr'
            msg = CFG['templates'][kind][lang]
            call('POST', f'{c["id"]}/replies', {'message': msg})
            done.add(c['id'])
            sent += 1
            with LOG.open('a', encoding='utf-8') as f:
                f.write(f'- {dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")} replied ({kind}/{lang}) '
                        f'to @{c.get("username")} "{c.get("text", "")[:80]}"\n')
    STATE.write_text(json.dumps(sorted(done)) + '\n')
    if flagged:
        open_issue('Comments to check by hand', '\n'.join(flagged))
    print(f'replies sent: {sent}, flagged: {len(flagged)}')


if __name__ == '__main__':
    main()
