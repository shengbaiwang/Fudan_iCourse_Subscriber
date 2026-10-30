from io import BytesIO
import json
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from serve_note_library import fixture
from local_web.notebook import Notebook
from local_web.note_library import NoteLibrary
from src.data.database import Database


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setenv('SEA_DISABLE_KEYCHAIN', '1')
    monkeypatch.setenv('ICOURSE_WEB_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('ICOURSE_WEB_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.delenv('SEA_DESKTOP_TOKEN', raising=False)
    app, library, personal, talk, database = fixture(tmp_path)
    try:
        yield app, library, personal, talk, database
    finally:
        database.close()


def test_live_sources_search_versions_and_no_generated_copies(sample):
    app, library, personal, talk, database = sample
    rows = library.list()['notes']
    assert len(rows) == 3
    assert {row['kind'] for row in rows} == {'personal', 'course', 'talk'}
    assert all('body' not in row and 'transcript' not in row for row in rows)
    assert len(library.list('档案')['notes']) == 3
    assert library.list('近代中国史')['notes'][0]['source_id'] == 'one'
    assert library.list(kind='talk')['notes'][0]['title'] == '城市空间讲座'
    assert len(library.notebook.list()) == 1
    course = library.get('course', 'one')
    assert course['versions'][0]['active'] and '课程当前正文' in course['body']
    assert any('旧版本' in row['summary'] for row in course['versions'])
    assert course['transcript'].startswith('课堂转写')
    assert library.get('talk', talk['id'])['transcript'].startswith('讲座转写')
    with pytest.raises(KeyError): library.get('course', 'pending')
    source = Database(str(database.db_path))
    source.update_summary('one', '重新生成后，统一入口立即读取新正文。', 'fixture/new')
    source.conn.close()
    assert library.get('course', 'one')['body'].startswith('重新生成后')
    assert len(library.notebook.list()) == 1
    library.talks.delete_talk(talk['id'])
    assert library.list(kind='talk')['notes'] == []


def test_manual_edits_conflicts_refresh_and_backup(sample, tmp_path):
    from local_web.notebook import NoteConflict
    app, library, _, talk, database = sample
    for kind, source_id in [('course', 'one'), ('talk', talk['id'])]:
        original = library.get(kind, source_id)
        edited = library.save_edit(kind, source_id, '修订标题', '# 修订正文\n\n手动勘误检索词', original['edit_revision'])
        assert edited['manually_edited'] and edited['body'].startswith('# 修订正文')
        assert edited['versions'][0]['model'] == '手动修订'
        assert any(row['summary'] == original['body'] for row in edited['versions'])
        assert library.edit_versions(kind, source_id)[0]['body'] == original['body']
        with pytest.raises(NoteConflict):
            library.save_edit(kind, source_id, '过期', '错误覆盖', original['edit_revision'])
        with TestClient(app) as client:
            stale = client.put(f'/api/local/note-library/{kind}/{source_id}/content', json={
                'title': '过期', 'body': '错误覆盖', 'revision': original['edit_revision']})
            assert stale.status_code == 409
            assert client.get(f'/api/local/note-library/{kind}/{source_id}/versions').status_code == 200
    assert len(library.list('手动勘误检索词')['notes']) == 2
    source = Database(str(database.db_path))
    source.update_summary('one', '来源重新生成', 'fixture/new')
    source.conn.close()
    course = library.get('course', 'one')
    assert course['body'].startswith('# 修订正文')
    assert any(row['summary'] == '来源重新生成' for row in course['versions'])
    destination = Notebook(tmp_path / 'restore-edits')
    backup = library.notebook.backup()
    assert destination.restore(backup) == 3
    restored = NoteLibrary(destination, library.database, library.talks)
    assert restored.get('course', 'one')['body'] == course['body']
    assert restored.edit_versions('course', 'one')
    assert destination.restore(backup) == 0
    restored.save_edit('course', 'one', course['title'], '导入后的新修改', course['edit_revision'])
    assert destination.restore(backup) == 1
    assert restored.get('course', 'one')['body'] == '导入后的新修改'
    copy = next(row for row in destination.list() if row['body'] == course['body'])
    assert destination.versions(copy['id'])


def test_source_refresh_conflicts_with_first_edit_and_invalid_backup_rolls_back(sample, tmp_path):
    from local_web.notebook import NoteConflict
    _, library, _, _, database = sample
    row = library.get('course', 'one')
    source = Database(str(database.db_path))
    source.update_summary('one', '新生成内容', 'fixture/new')
    source.conn.close()
    with pytest.raises(NoteConflict):
        library.save_edit('course', 'one', row['title'], '过期修改', row['edit_revision'])
    row = library.get('course', 'one')
    library.save_edit('course', 'one', row['title'], '手动内容', row['edit_revision'])
    with ZipFile(BytesIO(library.notebook.backup())) as archive:
        document = json.loads(archive.read('丰饶之海.json'))
    document['source_edits'][0]['body'] = ['invalid']
    destination = Notebook(tmp_path / 'invalid-edits')
    with pytest.raises(ValueError):
        destination.restore(archive_for(document))
    assert destination.list() == []


def test_folder_locations_nesting_delete_undo_and_backup(sample, tmp_path):
    _, library, personal, talk, _ = sample
    parent = library.save_folder('历史研究')
    child = library.save_folder('档案', parent_id=parent['id'])
    rows = [('personal', personal['id']), ('course', 'one'), ('talk', talk['id'])]
    for kind, source_id in rows:
        note = library.move_note(kind, source_id, child['id'], 0)
        assert note['location_changed'] and note['location_revision'] == 1
        assert not library.move_note(kind, source_id, child['id'], 1)['location_changed']
        library.set_star(kind, source_id, True)
    assert len(library.list(folder=child['id'], starred=True)['notes']) == 3
    assert len(library.notebook.list()) == 1
    library.save_folder('史料', child['id'])
    snapshot = library.delete_folder(parent['id'])
    assert library.metadata()['folders'] == []
    assert len(library.list(folder='')['notes']) == 3
    library.restore_folder(snapshot)
    assert len(library.list(folder=child['id'])['notes']) == 3
    destination = Notebook(tmp_path / 'restore')
    backup = library.notebook.backup()
    assert destination.restore(backup) == 1
    restored = NoteLibrary(destination, library.database, library.talks)
    folders = restored.metadata()['folders']
    root = next(row for row in folders if row['name'] == '历史研究')
    leaf = next(row for row in folders if row['name'] == '史料')
    assert leaf['parent_id'] == root['id']
    assert len(restored.list(folder=leaf['id'], starred=True)['notes']) == 3
    assert destination.restore(backup) == 0
    existing = destination.get(personal['id'])
    destination.save(existing['id'], existing['title'], '本机后续修改', existing['revision'])
    assert destination.restore(backup) == 1
    assert len(restored.list(folder=leaf['id'])['notes']) == 4
    assert destination.get(personal['id'])['body'] == '本机后续修改'
    # Reimporting metadata never reverts a more recent explicit move.
    note = restored.get('personal', personal['id'])
    restored.move_note('personal', personal['id'], '', note['location_revision'])
    destination.restore(backup)
    assert restored.get('personal', personal['id'])['folder_id'] == ''


def test_folders_reject_cycles_duplicates_and_stale_undo(sample):
    from local_web.notebook import NoteConflict
    _, library, personal, _, _ = sample
    root = library.save_folder('研究')
    child = library.save_folder('档案', parent_id=root['id'])
    with pytest.raises(ValueError): library.save_folder('研究')
    with pytest.raises(ValueError): library.move_folder(root['id'], child['id'], '')
    with pytest.raises(ValueError): library.save_folder('../目录')
    library.move_folder(child['id'], '', root['id'])
    with pytest.raises(NoteConflict): library.move_folder(child['id'], root['id'], root['id'])
    moved = library.move_note('personal', personal['id'], root['id'], 0)
    with pytest.raises(NoteConflict): library.move_note('personal', personal['id'], child['id'], 0)
    snapshot = library.delete_folder(root['id'])
    current = library.get('personal', personal['id'])
    library.move_note('personal', personal['id'], child['id'], current['location_revision'])
    with pytest.raises(NoteConflict): library.restore_folder(snapshot)
    assert library.get('personal', personal['id'])['folder_id'] == child['id']
    assert len(library.metadata()['folders']) == 1
    assert moved['folder_id'] == root['id']


def archive_for(document):
    result = BytesIO()
    with ZipFile(result, 'w') as archive: archive.writestr('丰饶之海.json', json.dumps(document))
    return result.getvalue()


def test_legacy_topics_migrate_once_and_multiple_choices_survive(sample, tmp_path):
    _, library, personal, _, _ = sample
    book = library.notebook
    key = 'personal:' + personal['id']
    with book.connect() as db, db:
        db.execute("DELETE FROM note_organization_state")
        db.executemany("INSERT INTO note_topics VALUES (?,?)", [('a'*32, '研究'), ('b'*32, '史料')])
        db.executemany("INSERT INTO note_topic_memberships VALUES (?,?)", [(key, 'a'*32), (key, 'b'*32), ('course:one', 'a'*32)])
    migrated = NoteLibrary(Notebook(book.directory.parent), library.database, library.talks)
    note = migrated.get('personal', personal['id'])
    assert note['folder_id'] == '' and len(note['folder_candidates']) == 2
    assert migrated.get('course', 'one')['folder_id'] == 'a'*32
    selected = migrated.move_note('personal', personal['id'], 'a'*32, note['location_revision'])
    assert selected['folder_candidates'] == []
    # Undo restores the unresolved choices as well as the inbox location.
    migrated.move_note('personal', personal['id'], '', selected['location_revision'], note['folder_candidates'])
    destination = Notebook(tmp_path / 'restored')
    destination.restore(book.backup())
    restored = NoteLibrary(destination, library.database, library.talks)
    assert len(restored.get('personal', personal['id'])['folder_candidates']) == 2
    assert len(Notebook(book.directory.parent).list()) == 1
    assert len(migrated.metadata()['folders']) == 2


def test_metadata_restore_is_atomic_and_old_backups_work(sample, tmp_path):
    _, library, personal, _, _ = sample
    with ZipFile(BytesIO(library.notebook.backup())) as archive:
        document = json.loads(archive.read('丰饶之海.json'))
    document['version'] = 1
    for field in ('folders', 'locations', 'folder_candidates', 'source_stars'): document.pop(field)
    destination = Notebook(tmp_path / 'restored')
    assert destination.restore(archive_for(document)) == 1
    document['topics'] = [{'id':'a'*32, 'name':'旧专题'}]
    document['memberships'] = [{'reference':'personal:' + personal['id'], 'topic_id':'a'*32}]
    legacy = Notebook(tmp_path / 'legacy')
    assert legacy.restore(archive_for(document)) == 1
    assert NoteLibrary(legacy, library.database, library.talks).get('personal', personal['id'])['folder_id'] == 'a'*32
    document['memberships'] = [{'reference':'invalid:one', 'topic_id':'a'*32}]
    fresh = Notebook(tmp_path / 'fresh')
    with pytest.raises(ValueError): fresh.restore(archive_for(document))
    assert fresh.list() == []
    assert NoteLibrary(fresh, library.database, library.talks).metadata()['folders'] == []
    document.update(version=2, folders=[{'id':'a'*32, 'name':'环', 'parent_id':'a'*32}], locations=[])
    with pytest.raises(ValueError): fresh.restore(archive_for(document))
    assert fresh.list() == []


def test_library_http_security_missing_sources_and_personal_conflict(sample, monkeypatch):
    app, library, personal, _, _ = sample
    monkeypatch.setenv('SEA_DESKTOP_TOKEN', 'fixture-session')
    with TestClient(app) as client:
        assert client.get('/api/local/note-library').status_code == 403
        client.headers['X-Sea-Session'] = 'fixture-session'
        assert len(client.get('/api/local/note-library').json()['notes']) == 3
        assert client.get('/api/local/note-library/course/missing').status_code == 404
        assert client.get('/api/local/note-library?kind=invalid').status_code == 400
        folder = client.post('/api/local/note-folders', json={'name':'同一研究问题'}).json()
        path = f"/api/local/note-library/personal/{personal['id']}"
        assert client.put(f"{path}/folder", json={'folder_id':folder['id'], 'revision':0}).status_code == 200
        assert client.put(f"{path}/folder", json={'folder_id':'', 'revision':0}).status_code == 409
        assert client.put(f"{path}/star", json={'value':True}).json()['starred']
        stale = client.put(f"/api/local/notebook/{personal['id']}", json={**personal,'body':'旧编辑器'})
        assert stale.status_code == 409
        assert client.put('/api/local/note-library/course/one/star', json={'value':'false'}).status_code == 422
        snapshot = client.delete(f"/api/local/note-folders/{folder['id']}").json()
        assert client.post('/api/local/note-folders/restore', json=snapshot).status_code == 200
        assert client.put(f"/api/local/note-folders/{folder['id']}", json={'name':' '}).status_code == 400


def add_course_notes(database):
    source = Database(str(database.db_path))
    source.insert_lecture('two', 'history', '第二讲', '2026-10-28')
    source.update_summary('two', '第二课次关键词：工业化与城市。', 'fixture/current')
    source.upsert_course('other', '世界史', '另一位教师')
    source.insert_lecture('other-one', 'other', '第一讲', '2026-10-27')
    source.update_summary('other-one', '另一门课程的独立笔记。', 'fixture/current')
    source.conn.close()


def test_courses_have_one_location_and_new_lectures_inherit_it(sample, tmp_path):
    _, library, _, _, database = sample
    add_course_notes(database)
    folder = library.save_folder('历史研究')
    moved = library.move_note('course-group', 'history', folder['id'], 0)
    assert moved['id'] == moved['location_reference'] == 'course-group:history'
    rows = library.list(kind='course', folder=folder['id'])['notes']
    assert {row['source_id'] for row in rows} == {'one', 'two'}
    assert {row['location_reference'] for row in rows} == {'course-group:history'}
    assert library.get('course', 'other-one')['folder_id'] == ''
    assert len(library.list('第二课次关键词')['notes']) == 1
    # Even a move requested through an individual lecture applies to the course.
    library.move_note('course', 'two', '', moved['location_revision'])
    assert library.get('course', 'one')['folder_id'] == ''
    group = library.get('course-group', 'history')
    library.move_note('course-group', 'history', folder['id'], group['location_revision'])
    source = Database(str(database.db_path))
    source.update_summary('pending', '之后新生成的课次。', 'fixture/new')
    source.conn.close()
    assert library.get('course', 'pending')['folder_id'] == folder['id']
    with library.notebook.connect() as db:
        assert not db.execute("SELECT 1 FROM note_locations WHERE reference LIKE 'course:%'").fetchone()
    destination = Notebook(tmp_path / 'course-restore')
    assert destination.restore(library.notebook.backup()) == 1
    restored = NoteLibrary(destination, library.database, library.talks)
    assert len(restored.list(kind='course', folder=folder['id'])['notes']) == 3
    assert len(restored.notebook.list()) == 1
    snapshot = library.delete_folder(folder['id'])
    assert {row['reference'] for row in snapshot['locations']} == {'course-group:history'}
    assert all(row['folder_id'] == '' for row in library.list(kind='course')['notes'])
    library.restore_folder(snapshot)
    assert len(library.list(kind='course', folder=folder['id'])['notes']) == 3


def test_old_lecture_placements_merge_and_preserve_conflicting_choices(sample, tmp_path):
    _, library, _, _, database = sample
    add_course_notes(database)
    first, second = library.save_folder('研究'), library.save_folder('阅读')
    with library.notebook.connect() as db, db:
        db.executemany("INSERT INTO note_locations VALUES (?,?,?)", [
            ('course:one', first['id'], 2), ('course:two', second['id'], 4),
            ('course:pending', first['id'], 1), ('course:unavailable', first['id'], 1),
        ])
    # Backups from the previous folder implementation migrate on restore too.
    destination = Notebook(tmp_path / 'old-course-restore')
    destination.restore(library.notebook.backup())
    restored = NoteLibrary(destination, library.database, library.talks)
    # Restore starts location revisions at 1; migration advances either store.
    for book, revision in ((library, 5), (restored, 2)):
        group = book.get('course-group', 'history')
        assert group['folder_id'] == '' and set(group['folder_candidates']) == {first['id'], second['id']}
        assert group['location_revision'] == revision
        for note in book.list(kind='course')['notes']:
            if note['course_id'] == 'history':
                assert note['folder_candidates'] == group['folder_candidates']
        from local_web.notebook import NoteConflict
        with pytest.raises(NoteConflict): book.move_note('course', 'one', second['id'], revision - 1)
        book.move_note('course-group', 'history', first['id'], group['location_revision'])
        assert {row['folder_id'] for row in book.list(kind='course')['notes'] if row['course_id'] == 'history'} == {first['id']}
        with book.notebook.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM note_locations WHERE reference LIKE 'course:%'").fetchone()[0] == 1
        # Re-reading never repeats migration or restores obsolete placements.
        assert book.get('course', 'two')['folder_candidates'] == []


def test_course_group_http_moves_all_lectures_and_rejects_stale_positions(sample):
    app, library, _, _, database = sample
    add_course_notes(database)
    with TestClient(app) as client:
        folder = client.post('/api/local/note-folders', json={'name':'历史'}).json()
        course = client.get('/api/local/note-library/course-group/history').json()
        assert course['title'] == '近代中国史'
        path = '/api/local/note-library/course-group/history/folder'
        moved = client.put(path, json={'folder_id':folder['id'], 'revision':0})
        assert moved.status_code == 200
        assert client.put('/api/local/note-library/course/two/folder', json={'folder_id':'', 'revision':0}).status_code == 409
        assert {row['source_id'] for row in client.get('/api/local/note-library', params={'folder':folder['id']}).json()['notes']} == {'one','two'}
        assert client.get('/api/local/note-library/course-group/missing').status_code == 404
