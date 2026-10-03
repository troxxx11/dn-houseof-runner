#!/usr/bin/env python3
"""Cloud poster for @houseof.no1se (GitHub Actions, always-on).

Each run stays alive up to ~5.5 h: it sleeps until the next due slot in queue.json, posts it at
the exact time, logs it in posted.jsonl (committed right away), and repeats. At the end the
workflow re-dispatches itself, so one run is always waiting (GitHub's own cron is too late to rely
on). Reel files are assets of this repo's public "queue" release, so Meta fetches them directly.
Env: FB_API, FB_IG_ID, FB_PAGE_TOKEN (secrets), GITHUB_REPOSITORY.
"""
import os, json, time, subprocess, datetime, urllib.request, urllib.parse

FB, IG, TOK = os.environ['FB_API'], os.environ['FB_IG_ID'], os.environ['FB_PAGE_TOKEN']
REPO = os.environ.get('GITHUB_REPOSITORY', 'troxxx11/dn-houseof-runner')
MIN_GAP_H = 0.9
LATE_LIMIT_H = 3        # a slot more than 3 h overdue is not posted (no posts at odd hours)
RUN_FOR_H = 5.5         # stay alive this long, then hand over to the next run
UTC = datetime.timezone.utc


def now():
    return datetime.datetime.now(UTC)


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


def git(*a):
    return subprocess.run(['git', *a], capture_output=True, text=True)


def refresh():
    git('pull', '--rebase', '--quiet')
    q = json.load(open('queue.json'))
    done = [json.loads(l) for l in open('posted.jsonl') if l.strip()] if os.path.exists('posted.jsonl') else []
    return q, done


def failed_keys():
    return {json.loads(l)['key'] for l in open('failed.jsonl') if l.strip()} if os.path.exists('failed.jsonl') else set()


def next_slot(q, done):
    keys = {d['key'] for d in done} | failed_keys()
    t = now()
    cand = [s for s in q['slots'] if s['key'] not in keys and s.get('status', 'ready') == 'ready'
            and (t - datetime.datetime.fromisoformat(s['due_at'])).total_seconds() < LATE_LIMIT_H * 3600]
    return sorted(cand, key=lambda x: x['due_at'])[0] if cand else None


def publish(s):
    url = f"https://github.com/{REPO}/releases/download/queue/{s['asset']}"
    c = api(f"{IG}/media", {'media_type': 'REELS', 'video_url': url, 'caption': s['caption'],
                            'share_to_feed': 'true', 'audio_name': s['audio_name']}, 'POST')
    if 'id' not in c: raise RuntimeError(f"container failed: {c}")
    sc, st = None, {}
    for _ in range(60):
        time.sleep(6)
        st = api(c['id'], {'fields': 'status_code,status'}); sc = st.get('status_code')
        if sc != 'IN_PROGRESS': break
    if sc != 'FINISHED': raise RuntimeError(f"processing ended {sc}: {st.get('status')}")
    r = api(f"{IG}/media_publish", {'creation_id': c['id']}, 'POST')
    if 'id' not in r: raise RuntimeError(f"publish failed: {r}")
    time.sleep(4)
    d = api(r['id'], {'fields': 'permalink,media_audio_type,timestamp'})
    return {'media_id': r['id'], 'permalink': d.get('permalink'), 'media_audio_type': d.get('media_audio_type')}


def log_post(s, res):
    rec = {'key': s['key'], 'n': s.get('n'), 'jarvis_id': s.get('jarvis_id'), 'posted_at': now().isoformat(timespec='seconds'),
           'track': s.get('track'), 'by': 'cloud', **res}
    with open('posted.jsonl', 'a') as f: f.write(json.dumps(rec) + '\n')
    git('config', 'user.name', 'dn-runner'); git('config', 'user.email', 'dn-runner@users.noreply.github.com')
    git('add', 'posted.jsonl'); git('commit', '-m', f"posted {s['key']}", '--quiet')
    for _ in range(3):
        git('pull', '--rebase', '--quiet')
        if git('push', '--quiet').returncode == 0: break
        time.sleep(5)
    print(json.dumps({'result': 'posted', **rec}), flush=True)


def main():
    deadline = now() + datetime.timedelta(hours=RUN_FOR_H)
    failures = 0
    while now() < deadline:
        q, done = refresh()
        if q.get('paused'):
            print('paused', flush=True); time.sleep(600); continue
        s = next_slot(q, done)
        if not s:
            time.sleep(min(600, max(1, (deadline - now()).total_seconds()))); continue
        due = datetime.datetime.fromisoformat(s['due_at'])
        if done:
            last = max(datetime.datetime.fromisoformat(d['posted_at']) for d in done)
            due = max(due, last + datetime.timedelta(hours=MIN_GAP_H))
        wait = (due - now()).total_seconds()
        if wait > 0:
            if due > deadline:
                time.sleep(max(1, (deadline - now()).total_seconds())); break
            time.sleep(min(wait, 600)); continue   # wake every 10 min to pick up pauses/skips
        try:
            log_post(s, publish(s)); failures = 0
        except Exception as e:
            failures += 1
            print(f"post failed ({failures}): {e}", flush=True)
            if failures >= 3:   # give up on this reel only; keep the runner alive for the next ones
                with open('failed.jsonl', 'a') as f:
                    f.write(json.dumps({'key': s['key'], 'at': now().isoformat(timespec='seconds'), 'error': str(e)[:300]}) + '\n')
                git('add', 'failed.jsonl'); git('commit', '-m', f"failed {s['key']}", '--quiet')
                git('pull', '--rebase', '--quiet'); git('push', '--quiet')
                failures = 0
            time.sleep(120)


if __name__ == '__main__':
    main()
