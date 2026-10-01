"""Daily snapshot of post and account metrics into insights.json (for the weekly analysis).
Needs the instagram_manage_insights permission on the token. Runs at most once per UTC day."""
import json, pathlib, datetime as dt
from publisher import call, ig_user_id, TOKEN

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'insights.json'
METRICS = ['reach', 'views', 'likes', 'comments', 'saved', 'shares', 'total_interactions',
           'ig_reels_avg_watch_time', 'ig_reels_video_view_total_time']


def main():
    if not TOKEN:
        return
    data = json.loads(OUT.read_text()) if OUT.exists() else {'snapshots': []}
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if data['snapshots'] and data['snapshots'][-1]['date'] == today:
        print('insights already taken today')
        return
    ig = ig_user_id()
    acct = call('GET', ig, {'fields': 'followers_count,media_count'})
    queue = json.loads((ROOT / 'queue.json').read_text(encoding='utf-8'))
    posts = {}
    for p in queue['posts']:
        if not p.get('media_id'):
            continue
        row = {'type': p['type'], 'trial': p.get('trial'), 'published_at': p.get('published_at')}
        for m in METRICS:
            if m.startswith('ig_reels') and p['type'] != 'reel':
                continue
            try:
                r = call('GET', f'{p["media_id"]}/insights', {'metric': m})
                row[m] = r['data'][0]['values'][0]['value'] if r.get('data') else None
            except Exception:
                row[m] = None          # metric not available for this media type / too early
        posts[p['id']] = row
    data['snapshots'].append({'date': today, 'followers': acct.get('followers_count'),
                              'media_count': acct.get('media_count'), 'posts': posts})
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n')
    print(f'insights snapshot {today}: {len(posts)} posts, {acct.get("followers_count")} followers')


if __name__ == '__main__':
    main()
