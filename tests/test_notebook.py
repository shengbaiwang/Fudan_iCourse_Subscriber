from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from local_web.notebook import Notebook, NoteConflict


def test_durable_notes_versions_conflicts_and_trash(tmp_path):
    book = Notebook(tmp_path)
    first = book.save(None, '  潮汐  ', '第一段')
    second = book.save(first['id'], '潮汐', '第二段', first['revision'])
    assert book.versions(first['id'])[0]['body'] == '第一段'
    with pytest.raises(NoteConflict):
        book.save(first['id'], '冲突标题', '另一窗口', first['revision'])
    assert book.get(first['id'])['body'] == '第二段'
    assert Notebook(tmp_path).get(first['id']) == second
    removed = book.save(second['id'], second['title'], second['body'], second['revision'], deleted=True)
    assert book.list() == []
    assert book.list(trash=True)[0]['id'] == first['id']
    book.save(removed['id'], removed['title'], removed['body'], removed['revision'], deleted=False)
    assert len(book.list('潮汐 第二段')) == 1
    assert len(book.versions(first['id'])) == 1
    assert book.path.stat().st_mode & 0o777 == 0o600


def test_backup_includes_markdown_history_and_trash_and_merges_safely(tmp_path):
    source = Notebook(tmp_path / 'a')
    row = source.save(None, '../潮汐', '正文 $x^2$')
    row = source.save(row['id'], row['title'], '修订内容', row['revision'])
    deleted = source.save(None, '已删手记', '保留')
    source.save(deleted['id'], deleted['title'], deleted['body'], deleted['revision'], deleted=True)
    archive = source.backup()
    with ZipFile(BytesIO(archive)) as zip:
        paths = zip.namelist()
        assert len([name for name in paths if name.endswith('.md')]) == 1
        assert all('..' not in name and not name.startswith('/') for name in paths)
    destination = Notebook(tmp_path / 'b')
    assert destination.restore(archive) == 2
    assert len(destination.list(trash=True)) == 1
    assert destination.versions(row['id'])[0]['body'] == '正文 $x^2$'
    assert destination.restore(archive) == 0
    existing = destination.get(row['id'])
    destination.save(existing['id'], existing['title'], '本机更新', existing['revision'])
    assert destination.restore(archive) == 1
    assert destination.get(existing['id'])['body'] == '本机更新'
    assert sorted(note['body'] for note in destination.list()) == ['修订内容', '本机更新']


def test_invalid_history_rolls_back_entire_import(tmp_path):
    source = Notebook(tmp_path / 'source')
    row = source.save(None, '手记', '原内容')
    source.save(row['id'], row['title'], '新内容', row['revision'])
    with ZipFile(BytesIO(source.backup())) as zip:
        document = json.loads(zip.read('丰饶之海.json'))
    document['versions'][0]['body'] = {'invalid': 'text'}
    stream = BytesIO()
    with ZipFile(stream, 'w') as zip:
        zip.writestr('丰饶之海.json', json.dumps(document))
    target = Notebook(tmp_path / 'target')
    with pytest.raises(ValueError):
        target.restore(stream.getvalue())
    assert target.list() == []


def test_history_bound_and_noop_writes(tmp_path):
    book = Notebook(tmp_path)
    row = book.save(None, '手记', '0')
    for index in range(65):
        row = book.save(row['id'], row['title'], str(index+1), row['revision'])
    assert len(book.versions(row['id'])) == 50
    assert book.save(row['id'], row['title'], row['body'], row['revision']) == row
