"""Approval from the owner's phone, through a private Telegram bot. Runs before publisher.py.

  1. Reads new button taps and messages (getUpdates; Telegram keeps them 24 h, we run every 15 min).
     The first /start pairs the bot with that private chat; nobody else can approve after that.
  2. Sends a preview (video or slides, the exact caption, Approve/Skip buttons) for each post waiting for approval.
  3. Reminds 2 h before a slot that has no answer yet.
  4. A post with no OK by its slot waits; 3 h after the slot it expires and is not posted.

Commands in the chat: /queue (next posts), /pause (stop all posting), /resume.
"""
import json, time, datetime as dt
import tg
from publisher import QUEUE, now, media_base, reachable

LATE = dt.timedelta(hours=3)        # an approval up to 3 h after the slot still posts
REMIND = dt.timedelta(hours=2)
MAX_PREVIEWS = 8                    # per run, so a big batch arrives over a few runs instead of one flood
HELP = ('I send you every post before it goes out on @sogixel.\n'
        '✅ Approve: it posts at its time.  ❌ Skip: it never posts.\n'
        'Changes show here within about 15 minutes.\n\n'
        '/queue  next posts\n/pause  stop all posting\n/resume  start again')


def at(p):
    return dt.datetime.fromisoformat(p['publish_at'])


def open_posts(q):
    return [p for p in q['posts'] if p['status'] in ('scheduled', 'retry')]


def queue_text(q, s):
    rows = []
    for p in sorted(open_posts(q), key=at)[:12]:
        mark = {'approved': '✅', 'skipped': '❌'}.get(p.get('approval'), '⏳')
        rows.append(f'{mark} {tg.when(p)} · {tg.kind(p)}\n    {p["id"]}')
    head = '⏸ Posting is PAUSED (/resume to restart)\n\n' if s['paused'] else ''
    return head + ('\n'.join(rows) if rows else 'Nothing scheduled.')


def handle_updates(q, s):
    cid = tg.chat_id(s)
    ups = tg.api('getUpdates', offset=s['offset'], timeout=0, allowed_updates=['message', 'callback_query'])
    if not cid:                                             # nothing identifying: the repo log is public
        print(f'not paired yet: {len(ups)} new message(s)')
    for u in ups:
        s['offset'] = u['update_id'] + 1
        if 'message' in u:
            m = u['message']
            chat, text = m['chat'], (m.get('text') or '').strip()
            if not cid and chat.get('type') == 'private' and text.startswith('/start'):
                s['chat'], cid = tg.seal(chat['id']), chat['id']
                s['paired_at'] = now().isoformat(timespec='seconds')
                tg.api('sendMessage', chat_id=cid, text='Paired ✅ This chat now approves @sogixel posts.\n\n' + HELP)
                print('paired with a private chat')
                continue
            if chat['id'] != cid:
                continue                                    # strangers get no answer
            cmd = text.split()[0].split('@')[0].lower() if text else ''
            if cmd == '/pause':
                s['paused'] = True
                reply = '⏸ Paused. Nothing will be posted until you send /resume.'
            elif cmd == '/resume':
                s['paused'] = False
                reply = '▶️ Resumed. Approved posts go out at their time.'
            elif cmd == '/queue':
                reply = queue_text(q, s)
            else:
                reply = HELP
            tg.api('sendMessage', chat_id=cid, text=reply)
        elif 'callback_query' in u:
            c = u['callback_query']
            if not cid or c['from']['id'] != cid:
                continue
            act, _, pid = c.get('data', '').partition(':')
            p = next((x for x in q['posts'] if x['id'] == pid), None)
            note = 'Not found'
            if p and p['status'] in ('scheduled', 'retry'):
                p['approval'] = 'approved' if act == 'ok' else 'skipped'
                p['approved_at'] = now().isoformat(timespec='seconds')
                note = 'Approved ✅' if act == 'ok' else 'Skipped ❌'
                tg.refresh(p, cid)
                print(f'{p["id"]}: {p["approval"]}')
            elif p:
                note = f'Already {p["status"]}'
            try:                                            # older than a few seconds: Telegram refuses, harmless
                tg.api('answerCallbackQuery', callback_query_id=c['id'], text=note)
            except Exception:
                pass
    return cid


def send_preview(p, cid, base):
    files = p['files'] + ([p['cover']] if p.get('cover') else [])
    if not all(reachable(base + f) for f in files):
        print(f'{p["id"]}: media not online yet, preview next run')
        return False
    try:
        if p['type'] == 'reel':
            tg.api('sendVideo', chat_id=cid, video=base + p['files'][0], supports_streaming='true')
        elif p['type'] == 'carousel':
            tg.api('sendMediaGroup', chat_id=cid, media=[{'type': 'photo', 'media': base + f} for f in p['files'][:10]])
        else:
            tg.api('sendPhoto', chat_id=cid, photo=base + p['files'][0])
    except Exception as e:                                  # still send the caption and buttons, with a link
        print(f'{p["id"]}: media preview failed: {e}')
        tg.api('sendMessage', chat_id=cid, text=f'(preview did not load) {base + p["files"][0]}')
    m = tg.api('sendMessage', chat_id=cid, text=tg.card(p), disable_web_page_preview='true', reply_markup=tg.keyboard(p))
    p['tg_msg'] = m['message_id']
    return True


def main():
    q = json.loads(QUEUE.read_text(encoding='utf-8'))
    s = tg.load()
    cid = None
    if tg.TOKEN:
        try:
            cid = handle_updates(q, s)
        except Exception as e:
            print('::warning::telegram updates failed:', e)
    else:
        print('TELEGRAM_BOT_TOKEN is not set: no previews sent; posts waiting for approval will not publish.')
    t = now()
    # expire unanswered or skipped posts once the late window has passed (works with or without Telegram)
    for p in open_posts(q):
        if s['require_approval'] and p.get('approval') != 'approved' and at(p) + LATE < t:
            p['status'] = 'skipped' if p.get('approval') == 'skipped' else 'expired'
            print(f'{p["id"]}: {p["status"]}')
            tg.refresh(p, cid)
            if cid and p['status'] == 'expired':
                tg.notify(f'⌛ {p["id"]} was not posted: no answer before {tg.when(p)}.')
    if cid and s['require_approval']:
        base, sent = media_base(), 0
        for p in sorted(open_posts(q), key=at):
            if p.get('approval') == 'pending' and not p.get('tg_msg') and sent < MAX_PREVIEWS:
                try:
                    if send_preview(p, cid, base):
                        sent += 1
                        time.sleep(1)
                except Exception as e:
                    print(f'::warning::preview {p["id"]} failed: {e}')
            elif (p.get('approval') == 'pending' and p.get('tg_msg') and not p.get('reminded')
                  and t < at(p) <= t + REMIND):
                try:
                    tg.api('sendMessage', chat_id=cid, reply_to_message_id=p['tg_msg'],
                           text=f'⏰ Goes out at {tg.when(p)[-5:]} only if you approve it.')
                    p['reminded'] = True
                except Exception as e:
                    print('reminder failed:', e)
        if sent:
            print(f'sent {sent} preview(s)')
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    tg.save(s)


if __name__ == '__main__':
    main()
