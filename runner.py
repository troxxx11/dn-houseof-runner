#!/usr/bin/env python3
"""Cloud poster for @houseof.no1se (runs in GitHub Actions every 30 min).

Reads queue.json (written by the Mac's sync_cloud.py), posts the oldest due slot that isn't
posted yet, logs it in posted.jsonl. The reel file (track already burned in) is a release asset
of this private repo; Meta needs a public URL, so it is staged on a temporary host for the fetch.
Env: FB_API, FB_IG_ID, FB_PAGE_TOKEN (repo secrets), GH_TOKEN (workflow token).
"""
import os, json, time, uuid, subprocess, datetime, urllib.request, urllib.parse

FB, IG, TOK = os.environ['FB_API'], os.environ['FB_IG_ID'], os.environ['FB_PAGE_TOKEN']
MIN_GAP_H = 0.9
LATE_LIMIT_H = 6  # a slot more than 6 h overdue waits for the next day's review instead of posting at odd hours


def api(path, params=None, method='GET'):
    p = dict(params or {}); p['access_token'] = TOK
    body = urllib.parse.urlencode(p)
    try:
        if method == 'GET':
            r = urllib.request.urlopen(f"{FB}/{path}?{body}", timeout=90)
        else:
            r = urllib.request.urlopen(urllib.request.Request(f"{FB}/{path}", data=body.encode(), method='POST'), timeout=180)
        return json.load(r)
    except urllib.error.HTTPError as e:
        return {'error_http': e.code, 'body': e.read().decode()[:300]}


def host(path):
    data = open(path, 'rb').read()
    for name, url, fields in [
        ('litterbox', 'https://litterbox.catbox.moe/resources/internals/api.php', {'reqtype': 'fileupload', 'time': '1h'}),
        ('catbox', 'https://catbox.moe/user/api.php', {'reqtype': 'fileupload'}),
    ]:
        try:
            b = uuid.uuid4().hex; body = b''
            for k, v in fields.items():
                body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            body += (f'--{b}\r\nContent-Disposition: form-data; name="fileToUpload"; filename="reel.mp4"\r\n'
                     f'Content-Type: video/mp4\r\n\r\n').encode() + data + f'\r\n--{b}--\r\n'.encode()
            u = urllib.request.urlopen(urllib.request.Request(url, data=body, headers={
                'Content-Type': f'multipart/form-data; boundary={b}', 'User-Agent': 'Mozilla/5.0'}), timeout=300).read().decode().strip()
            h = urllib.request.urlopen(urllib.request.Request(u, method='HEAD', headers={'User-Agent': 'curl/8.7.1'}), timeout=60)
            if h.status == 200 and 'video' in (h.headers.get('Content-Type') or ''):
                return u, name
        except Exception as e:
            print(f"host {name} failed: {str(e)[:120]}")
    raise RuntimeError('no host accepted the file')


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    q = json.load(open('queue.json'))
    if q.get('paused'):
        print('paused'); return
    done = [json.loads(l) for l in open('posted.jsonl') if l.strip()] if os.path.exists('posted.jsonl') else []
    done_keys = {d['key'] for d in done}
    if done:
        last = max(datetime.datetime.fromisoformat(d['posted_at']) for d in done)
        if (now - last).total_seconds() < MIN_GAP_H * 3600:
            print('gap: last post too recent'); return
    due = [s for s in q['slots'] if s['key'] not in done_keys and s.get('status', 'ready') == 'ready'
           and datetime.datetime.fromisoformat(s['due_at']) <= now
           and (now - datetime.datetime.fromisoformat(s['due_at'])).total_seconds() < LATE_LIMIT_H * 3600]
    if not due:
        print('nothing due'); return
    s = sorted(due, key=lambda x: x['due_at'])[0]
    subprocess.run(['gh', 'release', 'download', 'queue', '-p', s['asset'], '-D', 'dl', '--clobber'], check=True)
    url, hostname = host(os.path.join('dl', s['asset']))
    c = api(f"{IG}/media", {'media_type': 'REELS', 'video_url': url, 'caption': s['caption'],
                            'share_to_feed': 'true', 'audio_name': s['audio_name']}, 'POST')
    if 'id' not in c: raise SystemExit(f"container failed: {c}")
    sc, st = None, {}
    for _ in range(50):
        time.sleep(6)
        st = api(c['id'], {'fields': 'status_code,status'}); sc = st.get('status_code')
        if sc != 'IN_PROGRESS': break
    if sc != 'FINISHED': raise SystemExit(f"processing ended {sc}: {st.get('status')}")
    r = api(f"{IG}/media_publish", {'creation_id': c['id']}, 'POST')
    if 'id' not in r: raise SystemExit(f"publish failed: {r}")
    time.sleep(4)
    d = api(r['id'], {'fields': 'permalink,media_audio_type,timestamp'})
    rec = {'key': s['key'], 'n': s.get('n'), 'jarvis_id': s.get('jarvis_id'), 'posted_at': now.isoformat(timespec='seconds'),
           'track': s.get('track'), 'media_id': r['id'], 'permalink': d.get('permalink'),
           'media_audio_type': d.get('media_audio_type'), 'host': hostname, 'by': 'cloud'}
    with open('posted.jsonl', 'a') as f: f.write(json.dumps(rec) + '\n')
    print(json.dumps({'result': 'posted', **rec}))


if __name__ == '__main__':
    main()
