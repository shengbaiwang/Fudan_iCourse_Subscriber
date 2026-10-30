"""Vault acceptance: all data and credentials are isolated from the daily app."""
import json
from pathlib import Path
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from serve_note_library import fixture
from local_web.shared_notes import SharedNotes
from local_web.notebook import NoteConflict
from local_web.obsidian_appearance import install as install_appearance, render_snippet
from src.data.database import Database


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv('SEA_DISABLE_KEYCHAIN', '1')
    monkeypatch.setenv('ICOURSE_WEB_CONFIG_DIR', str(tmp_path / 'profile'))
    monkeypatch.setenv('ICOURSE_WEB_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.delenv('SEA_DESKTOP_TOKEN', raising=False)
    root = tmp_path / 'vault'
    root.mkdir()
    (root / '.obsidian').mkdir()
    monkeypatch.setenv('SEA_SHARED_NOTES_DIR', str(root))
    app, library, personal, talk, database = fixture(tmp_path)
    shared = app.state.shared_notes
    try:
        yield root, app, shared, personal, talk, database
    finally:
        shared.close()
        database.close()


def sync(shared):
    shared.call(lambda: None)


def note_path(root, shared, key):
    return root / shared.library.shared_paths[key]


def test_running_shared_vault_refreshes_enabled_appearance(vault):
    root, app, shared, _, _, _ = vault
    target = install_appearance(root)
    target.write_text("/* older palette */")
    expected = render_snippet()
    with TestClient(app):
        deadline = time.monotonic() + 5
        while target.read_text() != expected and time.monotonic() < deadline:
            time.sleep(.05)
        assert target.read_text() == expected


def test_migration_nested_external_edit_move_rename_create_and_restart(vault):
    root, app, shared, personal, talk, _ = vault
    parent = shared.library.save_folder('历史研究')
    row = shared.library.get('personal', personal['id'])
    shared.library.move_note('personal', personal['id'], parent['id'], row['location_revision'])
    sync(shared)
    assert len([path for path in root.rglob('*.md') if '.sea' not in path.relative_to(root).parts]) == 3
    assert (root / '近代中国史').is_dir()
    path = note_path(root, shared, 'personal:' + personal['id'])
    assert path.parent.name == '历史研究'
    raw = path.read_text()
    assert 'sea_id:' in raw
    path.write_text(raw + '\n外部编辑')
    child = root / '历史研究' / '档案'
    child.mkdir()
    path.rename(child / '改名后的札记.md')
    (child / 'Obsidian 新笔记.md').write_text('---\ntags: [历史]\n---\n新建正文')
    sync(shared)
    row = shared.library.get('personal', personal['id'])
    assert row['title'] == '改名后的札记'
    assert row['body'].endswith('外部编辑')
    assert len(shared.book.list()) == 2
    assert 'tags:' in (child / 'Obsidian 新笔记.md').read_text()
    (root / '历史研究').rename(root / '研究')
    sync(shared)
    assert note_path(root, shared, row['id']).is_file()
    assert '研究/档案' in shared.library.shared_paths[row['id']]
    restarted = SharedNotes(shared.library, root)
    sync(restarted)
    assert len(restarted.book.list()) == 2
    assert restarted.library.shared_paths[row['id']].startswith('研究/')


def test_app_edits_moves_folder_attachments_and_stale_save(vault):
    root, app, shared, personal, _, _ = vault
    sync(shared)
    key = 'personal:' + personal['id']
    path = note_path(root, shared, key)
    path.write_text(path.read_text() + '\nObsidian 新内容')
    with TestClient(app) as client:
        response = client.put('/api/local/notebook/' + personal['id'], json={'title': personal['title'], 'body': '过期草稿', 'revision': personal['revision']})
        assert response.status_code == 409
        row = client.get('/api/local/note-library/personal/' + personal['id']).json()
        assert row['body'].endswith('Obsidian 新内容')
        assert row['obsidian_url'].startswith('obsidian://open?path=')
        edited = client.put('/api/local/notebook/' + personal['id'], json={'title': '新标题', 'body': row['body'] + '\n应用编辑', 'revision': row['revision']})
        assert edited.status_code == 200, edited.text
        assert note_path(root, shared, key).name == '新标题.md'
        assert not path.exists()
        folder = client.post('/api/local/note-folders', json={'name': '资料'}).json()
        row = client.get('/api/local/note-library/personal/' + personal['id']).json()
        response = client.put('/api/local/note-library/personal/' + personal['id'] + '/folder', json={'folder_id': folder['id'], 'revision': row['location_revision']})
        assert response.status_code == 200, response.text
        assert note_path(root, shared, key).parent.name == '资料'
        (root / '资料' / '附件.png').write_bytes(b'image')
        assert client.put('/api/local/note-folders/' + folder['id'], json={'name': '史料'}).status_code == 200
        assert (root / '史料' / '附件.png').read_bytes() == b'image'
        assert note_path(root, shared, key).parent.name == '史料'


def test_generated_updates_remain_candidates_and_source_deletion_recovery(vault):
    root, app, shared, _, _, database = vault
    sync(shared)
    original = shared.library.get('course', 'one')['body']
    source = Database(str(database.db_path))
    source.update_summary('one', 'AI 新生成的候选正文', 'fixture/new')
    source.conn.close()
    sync(shared)
    row = shared.library.get('course', 'one')
    assert row['body'] == original
    assert any(item['summary'] == 'AI 新生成的候选正文' for item in row['versions'])
    path = note_path(root, shared, 'course:one')
    raw = path.read_text()
    path.unlink()
    sync(shared)
    assert shared.library.get('course', 'one')['deleted']
    assert any(row['id'] == 'course:one' for row in shared.library.list(trash=True)['notes'])
    assert not path.exists()
    shared.call(shared.restore, 'course:one')
    assert note_path(root, shared, 'course:one').is_file()
    assert shared.library.get('course', 'one')['body'] == original
    assert not shared.library.get('course', 'one')['deleted']


def test_personal_trash_restore_and_external_restore(vault):
    root, app, shared, personal, _, _ = vault
    sync(shared)
    key = 'personal:' + personal['id']
    path = note_path(root, shared, key)
    raw = path.read_text()
    path.unlink()
    sync(shared)
    assert shared.book.get(personal['id'])['deleted']
    path.write_text(raw + '\n恢复修改')
    sync(shared)
    assert not shared.book.get(personal['id'])['deleted']
    shared.call(shared.delete, key)
    assert not path.exists()
    shared.call(shared.restore, key)
    assert note_path(root, shared, key).is_file()
    assert not shared.book.get(personal['id'])['deleted']


def test_external_delete_restore_preserves_obsidian_properties(vault):
    root, _, shared, _, _, _ = vault
    (root / '带属性.md').write_text('---\ntags: [研究]\naliases: [旧名]\n---\n正文')
    sync(shared)
    row = next(row for row in shared.book.list() if row['title'] == '带属性')
    key = 'personal:' + row['id']
    path = note_path(root, shared, key)
    path.unlink()
    sync(shared)
    shared.call(shared.restore, key)
    raw = note_path(root, shared, key).read_text()
    assert shared.parse(raw)[0]['tags'] == ['研究']
    assert shared.parse(raw)[0]['aliases'] == ['旧名']


def test_simultaneous_edits_require_review_and_keep_history(vault):
    root, app, shared, personal, _, _ = vault
    sync(shared)
    key = 'personal:' + personal['id']
    row = shared.book.get(personal['id'])
    shared.book.save(row['id'], '应用修订', '应用的未同步内容', row['revision'])
    path = note_path(root, shared, key)
    path.write_text(path.read_text() + '\n外部修订')
    sync(shared)
    assert shared.conflicts[0]['id'] == key
    conflict = shared.call(shared.conflict, key)
    assert '外部修订' in conflict['file']['body']
    assert conflict['app']['body'] == '应用的未同步内容'
    shared.call(shared.resolve, key, '合并结果', '合并两端内容', conflict['raw_hash'], conflict['app_hash'])
    assert not shared.conflicts
    assert shared.book.get(row['id'])['body'] == '合并两端内容'
    assert note_path(root, shared, key).name == '合并结果.md'
    assert list((root / '.sea/history').rglob('*.md'))


def test_unavailable_vault_symlinks_duplicates_and_assets_are_safe(vault, tmp_path):
    root, app, shared, personal, _, _ = vault
    sync(shared)
    key = 'personal:' + personal['id']
    path = note_path(root, shared, key)
    raw = path.read_text()
    (root / '副本.md').write_text(raw)
    with pytest.raises(ValueError, match='重复'):
        sync(shared)
    assert shared.book.get(personal['id'])['body'] == personal['body']
    (root / '副本.md').unlink()
    outside = tmp_path / 'outside.md'
    outside.write_text('禁止读取')
    (root / '链接.md').symlink_to(outside)
    with pytest.raises(ValueError, match='符号链接'):
        sync(shared)
    assert outside.read_text() == '禁止读取'
    (root / '链接.md').unlink()
    (root / '照片.png').write_bytes(b'bitmap')
    sync(shared)
    assert shared.asset(key, '照片.png') == root / '照片.png'
    with pytest.raises(ValueError):
        shared.asset(key, '../../outside.png')
    moved = root.with_name('暂时移走')
    root.rename(moved)
    with pytest.raises(ValueError, match='暂不可用'):
        sync(shared)
    assert not shared.book.get(personal['id'])['deleted']


def test_folder_delete_undo_and_individual_course_placement(vault):
    root, app, shared, personal, _, _ = vault
    sync(shared)
    folder = shared.call(shared.library.save_folder, '临时')
    row = shared.library.get('course', 'one')
    shared.call(shared.library.move_note, 'course', 'one', folder['id'], row['location_revision'])
    assert note_path(root, shared, 'course:one').parent.name == '临时'
    snapshot = shared.call(shared.library.delete_folder, folder['id'])
    assert note_path(root, shared, 'course:one').parent == root
    assert not (root / '临时').exists()
    shared.call(shared.library.restore_folder, snapshot)
    assert note_path(root, shared, 'course:one').parent.name == '临时'


def test_duplicate_titles_keep_stable_paths_and_frontmatter(vault):
    root, _, shared, _, _, _ = vault
    shared.book.save(None, '同名', '第一个')
    shared.book.save(None, '同名', '第二个')
    sync(shared)
    paths = dict(shared.library.shared_paths)
    sync(shared)
    assert paths == shared.library.shared_paths
    for row in shared.book.list():
        if row['title'] == '同名':
            path = note_path(root, shared, 'personal:' + row['id'])
            path.write_text(path.read_text().replace('sea_id:', 'tags: [研究]\nsea_id:') + '\nObsidian 编辑')
    sync(shared)
    assert all(row['title'] == '同名' for row in shared.book.list() if row['body'].startswith(('第一个', '第二个')))
    assert paths == shared.library.shared_paths


def test_attachment_links_and_api_asset_auth(vault):
    root, app, shared, personal, _, _ = vault
    (root / '照片.png').write_bytes(b'bitmap')
    sync(shared)
    key = 'personal:' + personal['id']
    path = note_path(root, shared, key)
    path.write_text(path.read_text() + '\n![照片](照片.png)\n[自己](我的研究札记.md)')
    sync(shared)
    folder = shared.call(shared.library.save_folder, '子目录')
    row = shared.library.get('personal', personal['id'])
    shared.call(shared.library.move_note, 'personal', personal['id'], folder['id'], row['location_revision'])
    assert '](../' in shared.book.get(personal['id'])['body']
    with TestClient(app) as client:
        response = client.get('/api/local/shared-notes/asset', params={'note_id': key, 'target': '../照片.png'})
        assert response.status_code == 200, response.text
        assert response.json()['data_url'].startswith('data:image/png;base64,')
        assert client.get('/api/local/shared-notes/link', params={'note_id': key, 'target': '我的研究札记'}).json()['id'] == key


def test_external_folder_deletion_and_stale_conflict_resolution(vault):
    root, _, shared, _, _, _ = vault
    sync(shared)
    path = note_path(root, shared, 'course:one')
    path.unlink()
    (path.parent / '.sea-folder.json').unlink()
    path.parent.rmdir()
    sync(shared)
    assert shared.library.get('course', 'one')['deleted']
    assert all(row['name'] != '近代中国史' for row in shared.library.metadata()['folders'])


def test_renaming_note_updates_markdown_and_wikilinks(vault):
    root, _, shared, personal, _, _ = vault
    sync(shared)
    (root / '引用.md').write_text('[正文](我的研究札记.md)\n[[我的研究札记|别名]]\n`[[我的研究札记]]`')
    sync(shared)
    row = shared.book.get(personal['id'])
    shared.call(shared.save_personal, row['id'], '新名字', row['body'], row['revision'])
    body = (root / '引用.md').read_text()
    assert '[[新名字|别名]]' in body
    assert '%E6%96%B0%E5%90%8D%E5%AD%97.md' in body
    assert '`[[我的研究札记]]`' in body


def test_legacy_index_cannot_move_course_files_back_to_inbox(vault):
    root, _, shared, _, _, _ = vault
    sync(shared)
    path = note_path(root, shared, 'course:one')
    with shared.book.connect() as db, db:
        db.execute("DELETE FROM note_locations WHERE reference='course:one'")
    sync(shared)
    assert note_path(root, shared, 'course:one') == path
    assert shared.library.get('course', 'one')['folder_id']


def test_new_lectures_follow_renamed_course_directory(vault):
    root, _, shared, _, _, database = vault
    sync(shared)
    original = note_path(root, shared, 'course:one').parent
    original.rename(root / '重命名后的课程')
    sync(shared)
    source = Database(str(database.db_path))
    source.update_summary('pending', '新课次正文', 'fixture/new')
    source.upsert_course('new-course', '新增课程', '教师')
    source.insert_lecture('new-one', 'new-course', '新课次', '2026-10-28')
    source.update_summary('new-one', '另一门课的正文', 'fixture/new')
    source.conn.close()
    sync(shared)
    assert note_path(root, shared, 'course:pending').parent.name == '重命名后的课程'
    assert note_path(root, shared, 'course:new-one').parent.name == '新增课程'
    sync(shared)
    assert not original.exists()


def test_restart_keeps_individual_course_index(vault):
    root, _, shared, _, _, _ = vault
    from local_web.note_library import NoteLibrary
    sync(shared)
    folder = shared.call(shared.library.save_folder, '单独归档')
    row = shared.library.get('course', 'one')
    shared.call(shared.library.move_note, 'course', 'one', folder['id'], row['location_revision'])
    library = NoteLibrary(shared.book, shared.library.database, shared.library.talks)
    restarted = SharedNotes(library, root)
    assert library.metadata()['locations']['course:one']['folder_id'] == folder['id']
    sync(restarted)
    assert note_path(root, restarted, 'course:one').parent.name == '单独归档'


def test_existing_course_folder_does_not_gain_duplicate_level(vault):
    root, _, shared, _, _, _ = vault
    folder = shared.library.save_folder('近代中国史')
    shared.location('course-group:history', folder['id'])
    sync(shared)
    assert note_path(root, shared, 'course:one').parent == root / '近代中国史'
    assert len(shared.library.metadata()['folders']) == 1


def test_legacy_export_cannot_duplicate_shared_vault(vault):
    root, app, shared, _, _, _ = vault
    with TestClient(app) as client:
        response = client.post('/api/local/obsidian/preview', json={'vault_path':str(root)})
        assert response.status_code == 409
        assert '共享笔记库' in response.json()['detail']


def test_incomplete_yaml_pauses_without_stopping_later_sync(vault):
    root, _, shared, personal, _, _ = vault
    sync(shared)
    path = note_path(root, shared, 'personal:' + personal['id'])
    raw = path.read_text()
    path.write_text('---\ntags: [\n---\n尚在编辑')
    with pytest.raises(ValueError, match='YAML'):
        sync(shared)
    assert shared.book.get(personal['id'])['body'] == personal['body']
    path.write_text(raw + '\n完成修改')
    sync(shared)
    assert not shared.error
    assert shared.book.get(personal['id'])['body'].endswith('完成修改')
