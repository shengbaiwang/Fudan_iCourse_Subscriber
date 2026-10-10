"""Publish existing verified notes, then refresh the existing local website.

This entry point never starts ASR, OCR, summaries, or GitHub workflows.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_web.github_client import GitHubAPIError, GitHubClient
from local_web.state import SettingsStore
from scripts import local_course as runner
from scripts.publish_progress import publish_database, snapshot_database
from scripts.summarize_local_requests import complete_result
from src.ai.title import split_generated_title
from src.data.crypto_box import derive_new_password


def transient_error(exc):
    if isinstance(exc, GitHubAPIError):
        return exc.status in (0, 429) or exc.status >= 500
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


def sync_with_retries(creds, settings, base, client):
    # Retry synchronization only. Completed source/summary stages are untouched.
    for attempt in range(5):
        try:
            return sync_once(creds, settings, base, client)
        except Exception as exc:
            if not transient_error(exc) or attempt == 4:
                raise
            print(f'网页同步网络暂时中断；{type(exc).__name__}，第 {attempt + 1} 次重试。',
                  flush=True)
            time.sleep(min(30, 5 * (attempt + 1)))


def verified_delta(db, lectures, target: Path):
    """Copy completed, source-bound notes; leave the live checkpoint untouched."""
    notes = []
    for lecture in lectures:
        sid = str(lecture['sub_id'])
        row = db.get_lecture(sid) or {}
        if not lecture.get('has_playback') or not row.get('summary'):
            continue
        request = runner.summary_request(db, lecture)
        receipt_path = runner.RUN_DIR / 'summary-results' / f'{sid}.json'
        receipt = json.loads(receipt_path.read_text())
        if not complete_result(receipt, request):
            raise ValueError(f'{sid} 的完整摘要收据与材料不符')
        _, body = split_generated_title(receipt['text'])
        if body != row['summary'] or receipt['model'] != row['summary_model']:
            raise ValueError(f'{sid} 的笔记正文或模型与收据不符')
        pages = db.get_done_ppt_pages(sid)
        note = {'sub_id': sid, 'summary': row['summary'],
                'summary_model': row['summary_model'], 'ai_title': row.get('ai_title'),
                'processed_at': row.get('processed_at'), 'transcript': row['transcript'],
                'ppt_pages': pages, 'prompt_sha256': request['prompt_sha256']}
        notes.append(note)
    if not notes:
        return [], ''
    fingerprint = hashlib.sha256(json.dumps(notes, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    snapshot_database(db.db_path, target)
    ids = [note['sub_id'] for note in notes]
    placeholders = ','.join('?' for _ in ids)
    with sqlite3.connect(target) as conn:
        with conn:
            conn.execute(f'DELETE FROM lectures WHERE sub_id NOT IN ({placeholders})', ids)
            conn.execute(f'DELETE FROM ppt_pages WHERE sub_id NOT IN ({placeholders})', ids)
            conn.execute(f'DELETE FROM summary_versions WHERE sub_id NOT IN ({placeholders})', ids)
            conn.execute('DELETE FROM courses WHERE course_id != ?', (runner.COURSE_ID,))
            conn.execute('DELETE FROM all_courses WHERE course_id != ?', (runner.COURSE_ID,))
            conn.execute('DELETE FROM meta')
    target.chmod(0o600)
    return notes, fingerprint


def local_json(base, path, method='GET'):
    request = urllib.request.Request(base + path, method=method,
                                     data=b'' if method == 'POST' else None)
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def sync_once(creds, settings, local_url, client):
    state_path = runner.RUN_DIR / 'web-sync-state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    identity = {**settings.as_dict(), 'local_url': local_url, 'course_id': runner.COURSE_ID}
    if state.get('destination') != identity:
        state = {'destination': identity}
    manifest = json.loads((runner.RUN_DIR / 'manifest.json').read_text())
    lectures = runner.lecture_list(manifest)
    total = sum(bool(row.get('has_playback')) for row in lectures)
    with runner.read_snapshot(creds) as db, tempfile.TemporaryDirectory(prefix='icourse-note-sync-') as directory:
        target = Path(directory) / 'notes.db'
        notes, fingerprint = verified_delta(db, lectures, target)
        if not notes:
            return {'completed': 0, 'total': total, 'changed': False}
        changed = state.get('local_fingerprint') != fingerprint
        if not changed:
            return {'completed': len(notes), 'total': total, 'changed': False,
                    'commit_sha': state.get('commit_sha')}
        status = local_json(local_url, '/api/local/status')
        if not status.get('configured') or status.get('repository') != settings.as_dict():
            raise ValueError('本地网页连接与目标仓库不符')
        if state.get('cloud_fingerprint') != fingerprint:
            commit = publish_database(str(target), client=client,
                                      password=derive_new_password(creds.stuid, creds.uispsw),
                                      branch=settings.branch)
            state.update(cloud_fingerprint=fingerprint, commit_sha=commit)
            runner.write_private(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode())
        local_json(local_url, '/api/local/sync', 'POST')
        for note in notes:
            visible = local_json(local_url, '/api/local/lectures/' + note['sub_id'])
            versions = [{'summary': visible.get('summary'), 'model': visible.get('summary_model')},
                        *visible.get('summary_versions', [])]
            if not any(v.get('summary') == note['summary'] and v.get('model') == note['summary_model']
                       for v in versions):
                raise ValueError(f"网页未读到 {note['sub_id']} 的原笔记")
            if visible.get('transcript') != note['transcript']:
                raise ValueError(f"网页的 {note['sub_id']} 转录不符")
        state.update(local_fingerprint=fingerprint, synced_sub_ids=[n['sub_id'] for n in notes],
                     updated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), state='synced')
        runner.write_private(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode())
        return {'completed': len(notes), 'total': total, 'changed': True,
                'commit_sha': state['commit_sha'], 'local_url': local_url}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course-config', type=Path)
    parser.add_argument('--local-url', required=True)
    parser.add_argument('--watch', action='store_true', help='仅等待新笔记并同步，不运行处理任务')
    args = parser.parse_args()
    base = args.local_url.rstrip('/')
    parsed = urlparse(base)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or parsed.path or parsed.query or parsed.fragment or parsed.username):
        parser.error('--local-url 必须是现有本机网页的 http 地址')
    runner.configure_course(args.course_config)
    creds = runner.credentials()
    if not creds.token:
        raise ValueError('本机缺少已连接仓库的 Token')
    settings = SettingsStore().load()
    client = GitHubClient(settings.owner, settings.repo, creds.token, timeout=120)
    with open(runner.RUN_DIR / 'web-sync.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('这门课已有网页同步进程') from None
        while True:
            result = sync_with_retries(creds, settings, base, client)
            if result['changed']:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if result['completed'] == result['total']:
                print('全部已有笔记已同步到两个网页入口。', flush=True)
                return 0
            if not args.watch:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0
            time.sleep(15)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        status = exc.status if isinstance(exc, GitHubAPIError) else None
        print(f'网页同步失败（{type(exc).__name__}，HTTP {status}）；原处理进程不受影响。', file=sys.stderr)
        raise SystemExit(1)
