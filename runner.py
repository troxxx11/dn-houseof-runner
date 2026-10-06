#!/usr/bin/env python3
"""Cloud poster for @houseof.no1se (GitHub Actions, always-on).

Each run stays alive up to ~5.5 h: it sleeps until the next due slot in queue.json, posts it at
the exact time, logs it in posted.jsonl (committed right away), and repeats. At the end the
workflow re-dispatches itself, so one run is always waiting (GitHub's own cron is too late to rely
on). Reel files and carousel slides are assets of this repo's public "queue" release, so Meta
fetches them directly.
Slots: REELS (default; `asset`, `audio_name`) or media_type CAROUSEL (`images` = 2-10 asset names,
.jpg -> IMAGE child, .mp4 -> VIDEO child). Any slot may carry `collaborators` (IG usernames, Collab
invite; the invitee accepts in the app). Local checks: --dry-run [KEY] / --container-test KEY.
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


def asset_url(name):
    return f"https://github.com/{REPO}/releases/download/queue/{name}"


def with_collabs(params, s):
    """Optional Collab invite (Graph API `collaborators`, IG usernames). Each invitee accepts in the app."""
    users = [u.lstrip('@') for u in (s.get('collaborators') or []) if u]
    if users:
        params['collaborators'] = json.dumps(users)
    return params


def container_requests(s):
    """The container POSTs for a slot, in order: [(label, params)]. A CAROUSEL is its children
    (is_carousel_item=true; .mp4 = VIDEO child, else IMAGE child) and then the parent, whose
    `children` is filled in at run time. REELS (default) is one request, unchanged."""
    if s.get('media_type', 'REELS').upper() == 'CAROUSEL':
        reqs = []
        for name in s['images']:
            if name.lower().endswith(('.mp4', '.mov')):
                reqs.append(('child', {'media_type': 'VIDEO', 'video_url': asset_url(name), 'is_carousel_item': 'true'}))
            else:
                reqs.append(('child', {'image_url': asset_url(name), 'is_carousel_item': 'true'}))
        reqs.append(('parent', with_collabs({'media_type': 'CAROUSEL', 'children': None, 'caption': s['caption']}, s)))
        return reqs
    p = {'media_type': 'REELS', 'video_url': asset_url(s['asset']), 'caption': s['caption'],
         'share_to_feed': 'true', 'audio_name': s['audio_name']}
    return [('reel', with_collabs(p, s))]


def wait_finished(cid, tries=60):
    sc, st = None, {}
    for _ in range(tries):
        st = api(cid, {'fields': 'status_code,status'}); sc = st.get('status_code')
        if sc not in ('IN_PROGRESS', None): break
        time.sleep(6)
    if sc != 'FINISHED': raise RuntimeError(f"processing of {cid} ended {sc}: {st.get('status') or st}")


def create_container(s):
    """Create (and wait for) every container of the slot; returns the id to publish."""
    kids, cid = [], None
    for kind, params in container_requests(s):
        if kind == 'parent':
            if not 2 <= len(kids) <= 10: raise RuntimeError(f"carousel needs 2-10 children, got {len(kids)}")
            params = dict(params, children=','.join(kids))
        c = api(f"{IG}/media", params, 'POST')
        if 'id' not in c: raise RuntimeError(f"{kind} container failed: {c}")
        wait_finished(c['id'])
        if kind == 'child':
            kids.append(c['id'])
        else:
            cid = c['id']
    return cid


def publish(s):
    cid = create_container(s)
    r = api(f"{IG}/media_publish", {'creation_id': cid}, 'POST')
    if 'id' not in r: raise RuntimeError(f"publish failed: {r}")
    time.sleep(4)
    d = api(r['id'], {'fields': 'permalink,media_audio_type,media_type,timestamp'})
    return {'media_id': r['id'], 'permalink': d.get('permalink'), 'media_type': d.get('media_type'),
            'media_audio_type': d.get('media_audio_type'), 'collaborators': s.get('collaborators') or []}


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


def test_cli(argv):
    """Local checks, never publishes:
      runner.py --dry-run [KEY ...]      print the container requests for the slots (no API calls)
      runner.py --container-test KEY     create the real containers for KEY and stop before media_publish
    """
    q = json.load(open('queue.json'))
    keys = [a for a in argv[1:] if not a.startswith('--')]
    slots = [s for s in q['slots'] if not keys or s['key'] in keys]
    if argv[0] == '--dry-run':
        out = []
        for s in slots:
            reqs = [(k, dict(p, children='<child container ids>') if k == 'parent' else p)
                    for k, p in container_requests(s)]
            out.append({'key': s['key'], 'due_at': s['due_at'], 'status': s.get('status'),
                        'media_type': s.get('media_type', 'REELS'), 'requests': reqs})
        print(json.dumps(out, indent=1, ensure_ascii=False))
    elif argv[0] == '--container-test':
        for s in slots:
            cid = create_container(s)
            st = api(cid, {'fields': 'status_code,status'})
            print(json.dumps({'key': s['key'], 'container': cid, 'status': st, 'published': False}))


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('--dry-run', '--container-test'):
        test_cli(sys.argv[1:])
    else:
        main()
