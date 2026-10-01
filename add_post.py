"""Manage the posting queue locally (used by Claude through the ig-employee skill).

  add_post.py reel VIDEO.mp4 --caption-file C.txt --at "2026-10-02 13:00" [--cover COVER.jpg] [--id NAME] [--no-ai-label]
  add_post.py carousel S1.png S2.png ... --caption-file C.txt --at "2026-10-02 13:00" [--id NAME]
  add_post.py image IMG.png --caption-file C.txt --at "..."
  add_post.py list
  add_post.py remove ID        (only posts that are not published yet)
  add_post.py move ID "2026-10-03 20:00"
  add_post.py approve ID [ID ...]   (owner said yes in chat; Telegram buttons do the same)

New posts wait for the owner's OK (Telegram preview with Approve/Skip) unless --approved is given.

Times are Morocco time (UTC+1) unless an explicit offset is given. Images are converted to JPEG
(the Instagram API only accepts JPEG). Files are copied into media/<id>/ so GitHub Pages can serve them.
Then commit and push; GitHub Actions publishes when the time comes.
"""
import argparse, json, pathlib, shutil, subprocess, datetime as dt, re, sys

ROOT = pathlib.Path(__file__).resolve().parent
QUEUE = ROOT / 'queue.json'
TZ = dt.timezone(dt.timedelta(hours=1))   # Morocco (UTC+1). During Ramadan Morocco switches to UTC+0: pass an explicit offset then.


def load():
    return json.loads(QUEUE.read_text(encoding='utf-8')) if QUEUE.exists() else {'posts': []}


def save(q):
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def when(s):
    t = dt.datetime.fromisoformat(s.replace(' ', 'T'))
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    return t.isoformat(timespec='minutes')


def to_jpeg(src, dst):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-q:v', '2', str(dst)], check=True)


def add(a):
    q = load()
    pid = a.id or re.sub(r'[^a-z0-9-]+', '-', pathlib.Path(a.files[0]).stem.lower())
    if any(p['id'] == pid for p in q['posts']):
        pid = f'{pid}-{len(q["posts"]) + 1}'
    d = ROOT / 'media' / pid
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i, f in enumerate(a.files, 1):
        f = pathlib.Path(f).expanduser()
        if a.type == 'reel':
            dst = d / 'video.mp4'
            shutil.copy(f, dst)
        else:
            dst = d / f'{i:02d}.jpg'
            to_jpeg(f, dst)
        files.append(str(dst.relative_to(ROOT)))
    cover = None
    if a.cover:
        cdst = d / 'cover.jpg'
        to_jpeg(pathlib.Path(a.cover).expanduser(), cdst)
        cover = str(cdst.relative_to(ROOT))
    caption = pathlib.Path(a.caption_file).expanduser().read_text(encoding='utf-8').strip()
    if len(caption) > 2200:
        sys.exit('caption longer than 2200 characters')
    if a.type == 'carousel' and not 2 <= len(files) <= 10:
        sys.exit('a carousel needs 2 to 10 images')
    q['posts'].append({'id': pid, 'type': a.type, 'files': files, 'cover': cover, 'caption': caption,
                       'publish_at': when(a.at), 'ai_label': not a.no_ai_label, 'status': 'scheduled',
                       'trial': a.trial if a.type == 'reel' else None, 'story': a.story,
                       'approval': 'approved' if a.approved else 'pending', 'tag': a.tag})
    q['posts'].sort(key=lambda p: p['publish_at'])
    save(q)
    print('queued', pid, when(a.at))


def show(_):
    for p in load()['posts']:
        extra = p.get('permalink') or p.get('last_error', '')[:80]
        flags = ('T' if p.get('trial') else '-') + ('S' if p.get('story') else '-') + \
                {'approved': 'A', 'skipped': 'X'}.get(p.get('approval'), '?')
        print(f"{p['publish_at']}  {p['status']:<10} {p['type']:<8} {flags} {p['id']}  {extra}")


def remove(a):
    q = load()
    p = next(p for p in q['posts'] if p['id'] == a.pid)
    if p['status'] == 'published':
        sys.exit('already published; delete it on Instagram instead')
    q['posts'].remove(p)
    shutil.rmtree(ROOT / 'media' / a.pid, ignore_errors=True)
    save(q)
    print('removed', a.pid)


def move(a):
    q = load()
    p = next(p for p in q['posts'] if p['id'] == a.pid)
    p['publish_at'] = when(a.at)
    if p['status'] in ('failed', 'retry', 'expired', 'skipped'):
        if p['status'] in ('expired', 'skipped'):      # back to the phone for a fresh OK
            p.update(approval='pending', tg_msg=None)
        p['status'] = 'scheduled'
        p['attempts'] = 0
    p.pop('reminded', None)
    q['posts'].sort(key=lambda p: p['publish_at'])
    save(q)
    print('moved', a.pid, p['publish_at'])


ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest='cmd', required=True)
for t in ('reel', 'carousel', 'image'):
    s = sub.add_parser(t)
    s.add_argument('files', nargs='+')
    s.add_argument('--caption-file', required=True)
    s.add_argument('--at', required=True)
    s.add_argument('--cover')
    s.add_argument('--id')
    s.add_argument('--no-ai-label', action='store_true')
    s.add_argument('--trial', choices=['MANUAL', 'SS_PERFORMANCE'], help='reels only: publish as a trial reel')
    s.add_argument('--story', action='store_true', help='also repost to Stories (cover or first slide)')
    s.add_argument('--approved', action='store_true', help='owner already approved it in chat: no Telegram preview')
    s.add_argument('--tag', help='what the post answers, e.g. Q1..Q5 (buyer questions), for the weekly analysis')
    s.set_defaults(func=add, type=t)
sub.add_parser('list').set_defaults(func=show)
r = sub.add_parser('remove'); r.add_argument('pid'); r.set_defaults(func=remove)
m = sub.add_parser('move'); m.add_argument('pid'); m.add_argument('at'); m.set_defaults(func=move)


def setopt(a):
    q = load()
    p = next(p for p in q['posts'] if p['id'] == a.pid)
    if a.trial is not None:
        p['trial'] = None if a.trial == 'off' else a.trial
    if a.story is not None:
        p['story'] = a.story == 'on'
    save(q)
    print('updated', a.pid, 'trial=', p.get('trial'), 'story=', p.get('story'))


o = sub.add_parser('set'); o.add_argument('pid'); o.add_argument('--trial', choices=['off', 'MANUAL', 'SS_PERFORMANCE'])
o.add_argument('--story', choices=['on', 'off']); o.set_defaults(func=setopt)


def approve(a):
    """Record an approval the owner gave in chat (the Telegram buttons do the same)."""
    q = load()
    for pid in a.pids:
        p = next(p for p in q['posts'] if p['id'] == pid)
        p['approval'] = a.state
        print(pid, a.state)
    save(q)


for name, state in (('approve', 'approved'), ('unapprove', 'pending')):
    v = sub.add_parser(name); v.add_argument('pids', nargs='+'); v.set_defaults(func=approve, state=state)
a = ap.parse_args()
a.func(a)
