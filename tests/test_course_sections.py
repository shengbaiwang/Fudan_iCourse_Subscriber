import json
from pathlib import Path
from unittest.mock import patch

import pytest
import asyncio
from fastapi import HTTPException
from local_web.state import CourseZoneStore, RuntimeState, SettingsStore, validate_course_sections
from local_web.server import create_app, CourseSectionsRequest, CourseZoneRequest
from local_web.database import DatabaseManager
from src.data.database import Database
from src.data.departments import normalize_department, normalize_catalog_departments
from src.data.schema import SCHEMA_SQL
import sqlite3
import subprocess
from scripts.merge_db import merge
from src.data.sharder import shard_database, reassemble_database


class EmptyKeychain:
    available = False
    def load(self, settings):
        return None


def runtime(path):
    return RuntimeState(store=SettingsStore(path), credential_store=EmptyKeychain())


A = {"id": "section-" + "a" * 32, "name": "思想史"}
B = {"id": "section-" + "b" * 32, "name": "写作材料"}


def test_new_library_has_no_presets(tmp_path):
    state = runtime(tmp_path)
    assert state.course_organization() == {"zones": {}, "sections": [], "default_zone": "unassigned", "revision": 0}


DEPARTMENT_CASES = [
    (None, ""), ("", ""), (" \t　", ""),
    ("016 哲学学院", "哲学学院"), ("哲学学院", "哲学学院"),
    ("016 016 哲学学院", "哲学学院"),
    (" 014\t历史学系　", "历史学系"), ("068\u00a0经济学院", "经济学院"),
    ("０６８　经济学院", "经济学院"), ("305 计算与智能创新学院", "计算与智能创新学院"),
    ("复旦大学", "复旦大学"), ("016", "016"),
    ("2026 研究中心", "2026 研究中心"), ("016哲学学院", "016哲学学院"),
    ("中国语言文学系", "中国语言文学系"), ("外国语言文学学院", "外国语言文学学院"),
]


def test_department_rules_match_browser_and_are_idempotent():
    # One set of cases prevents Python/JS rules drifting apart.
    source = Path(__file__).resolve().parents[1] / "local_web/static/departments.js"
    output = subprocess.check_output([
        "node", "-e",
        "global.window = {}; require(process.argv[1]); "
        "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(window.ICS.normalizeDepartment)));",
        str(source), json.dumps([raw for raw, _ in DEPARTMENT_CASES]),
    ], text=True)
    assert json.loads(output) == [expected for _, expected in DEPARTMENT_CASES]
    for raw, expected in DEPARTMENT_CASES:
        assert normalize_department(raw) == expected
        assert normalize_department(expected) == expected


def test_old_catalog_repair_and_future_imports(tmp_path):
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO all_courses VALUES ('1', '2026-春', '课程', '教师', '016 哲学学院', 'original')")
    db = Database(str(path))
    try:
        assert db.list_all_courses()[0]["dept"] == "哲学学院"
        assert db.list_all_courses()[0]["last_seen_at"] == "original"
        db.upsert_all_courses_for_term("2026-秋", [
            {"course_id": "2", "dept": "016 哲学学院"},
            {"course_id": "3", "dept": "哲学学院"},
        ])
        normalize_catalog_departments(db.conn)
        assert len(db.list_all_courses()) == 3
        assert {row["dept"] for row in db.list_all_courses()} == {"哲学学院"}
        assert db.conn.execute("SELECT COUNT(*) FROM all_courses WHERE dept = '哲学学院'").fetchone()[0] == 3
    finally:
        db.conn.close()


def test_read_only_legacy_library_returns_canonical_departments(tmp_path):
    manager = DatabaseManager(tmp_path)
    try:
        with sqlite3.connect(manager.db_path) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO courses VALUES ('1', '课程', '教师')")
            conn.execute("INSERT INTO all_courses VALUES ('1', '2026-秋', '课程', '教师', '016 哲学学院', 'original')")
        assert manager.courses()[0]["dept"] == "哲学学院"
        assert manager.subscription_courses(["1"])[0]["dept"] == "哲学学院"
        catalog = manager.subscription_catalog("哲学学院")
        assert catalog["total"] == 1
        assert catalog["courses"][0]["dept"] == "哲学学院"
        # The read-only API must work without rewriting the encrypted cache.
        with sqlite3.connect(manager.db_path) as conn:
            assert conn.execute("SELECT dept FROM all_courses").fetchone()[0] == "016 哲学学院"
    finally:
        manager.close()


def test_merge_and_old_shards_cannot_restore_duplicate_departments(tmp_path):
    local, remote = tmp_path / "local.db", tmp_path / "remote.db"
    for path, cid, dept in [(local, "1", "014 历史学系"), (remote, "2", "历史学系")]:
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO courses VALUES (?, '课程', '教师')", (cid,))
            conn.execute("INSERT INTO all_courses VALUES (?, '2026-秋', '课程', '教师', ?, 'original')", (cid, dept))
    merge(str(local), str(remote))
    with sqlite3.connect(remote) as conn:
        assert conn.execute("SELECT DISTINCT dept FROM all_courses").fetchall() == [("历史学系",)]
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2
        # Simulate a catalog published by an old client.
        conn.execute("UPDATE all_courses SET dept = '014 历史学系' WHERE course_id = '1'")
    shard_dir = tmp_path / "sharded"
    index = shard_database(str(remote), str(shard_dir), "test-password")
    output = tmp_path / "restored.db"
    reassemble_database(index, str(shard_dir / "shards"), str(output), "test-password")
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT DISTINCT dept FROM all_courses").fetchall() == [("历史学系",)]
        assert conn.execute("SELECT COUNT(*) FROM all_courses").fetchone()[0] == 2
        assert conn.execute("SELECT DISTINCT last_seen_at FROM all_courses").fetchall() == [("original",)]


@pytest.mark.parametrize('wrapped', [False, True])
def test_legacy_migration_and_delete_all_preserves_archive(tmp_path, wrapped):
    legacy = {"1": "学习区", "2": "archive", "3": "整理区"}
    (tmp_path / 'course-zones.json').write_text(json.dumps({"zones": legacy} if wrapped else legacy))
    state = runtime(tmp_path)
    assert state.default_course_zone == 'unassigned'
    assert state.course_zones == {'1': 'study', '2': 'archive', '3': 'unassigned'}
    assert state.course_sections == [{'id': 'study', 'name': '学习区'}, {'id': 'reference', 'name': '资料库'}]
    state.save_course_sections([], 0)
    restored = runtime(tmp_path)
    assert restored.course_zones == {'1': 'unassigned', '2': 'archive', '3': 'unassigned'}
    assert restored.course_sections == []
    assert restored.default_course_zone == 'unassigned'
    assert restored.course_sections_revision == 1


def test_rename_reorder_persist_and_reject_deleted_destination(tmp_path):
    state = runtime(tmp_path)
    state.save_course_sections([A, B], 0)
    state.save_course_zone('1', A['id'])
    state.save_course_sections([B, {**A, 'name': '研究专题'}], 1)
    restored = runtime(tmp_path)
    assert [s['name'] for s in restored.course_sections] == ['写作材料', '研究专题']
    assert restored.course_zones['1'] == A['id']
    restored.save_course_sections([B], 2)
    with pytest.raises(ValueError):
        restored.save_course_zone('1', A['id'])
    assert runtime(tmp_path).course_zones['1'] == 'unassigned'


def test_existing_organization_migrates_without_losing_custom_sections(tmp_path):
    old = {'zones': {'1': 'organize', '2': 'reference', '3': A['id'], '4': 'archive'},
           'sections': [{'id': 'organize', 'name': '整理区'}, {'id': 'reference', 'name': '查阅区'}, A],
           'default_zone': 'reference', 'revision': 7}
    (tmp_path / 'course-zones.json').write_text(json.dumps(old))
    state = runtime(tmp_path)
    assert state.default_course_zone == 'unassigned'
    assert state.course_sections_revision == 7
    assert state.course_zones == {**old['zones'], '1': 'unassigned'}
    assert state.course_sections == [{'id': 'reference', 'name': '资料库'}, A]
    state.save_course_zone('5', 'unassigned')
    assert runtime(tmp_path).course_organization() == state.course_organization()
    # A user-renamed organize section remains an ordinary category.
    old['sections'][0]['name'] = '研究项目'
    (tmp_path / 'course-zones.json').write_text(json.dumps(old))
    assert runtime(tmp_path).course_zones['1'] == 'organize'
    assert runtime(tmp_path).course_sections[0]['name'] == '研究项目'


def test_reference_rename_preserves_existing_library_category(tmp_path):
    document = {'zones': {'1': 'reference', '2': A['id']}, 'sections': [
        {'id': 'reference', 'name': '查阅区'}, {**A, 'name': '资料库'}], 'revision': 2}
    (tmp_path / 'course-zones.json').write_text(json.dumps(document))
    state = runtime(tmp_path)
    assert [s['name'] for s in state.course_sections] == ['资料库 2', '资料库']
    assert state.course_zones == document['zones']


def test_nested_sections_keep_membership_when_moved_and_parent_deleted(tmp_path):
    state = runtime(tmp_path)
    child = {'id': 'section-' + 'c' * 32, 'name': '英语', 'parent_id': A['id']}
    state.save_course_sections([A, child, B], 0)
    state.save_course_zone('1', A['id'])
    state.save_course_zone('2', child['id'])
    state.save_course_sections([A, B, {**child, 'parent_id': B['id']}], 1)
    restored = runtime(tmp_path)
    assert restored.course_sections[-1]['parent_id'] == B['id']
    assert restored.course_zones == {'1': A['id'], '2': child['id']}
    # The UI promotes children when a parent is removed; direct members go to the inbox.
    state.save_course_sections([B, {**child, 'parent_id': B['id']}], 2)
    assert runtime(tmp_path).course_zones == {'1': 'unassigned', '2': child['id']}


def test_same_name_allowed_in_different_parents():
    child_a = {'id': 'section-' + 'c' * 32, 'name': '笔记', 'parent_id': A['id']}
    child_b = {'id': 'section-' + 'd' * 32, 'name': '笔记', 'parent_id': B['id']}
    assert validate_course_sections([A, B, child_a, child_b]) == [A, B, child_a, child_b]
    with pytest.raises(ValueError):
        validate_course_sections([A, B, child_a, {**child_b, 'parent_id': A['id']}])


@pytest.mark.parametrize('sections', [
    [{**A, 'parent_id': A['id']}],
    [{**A, 'parent_id': B['id']}, {**B, 'parent_id': A['id']}],
    [{**A, 'parent_id': B['id']}], [{**A, 'parent_id': 'unassigned'}],
    [{**A, 'parent_id': ''}], [{**A, 'name': '待整理'}],
])
def test_invalid_hierarchy_does_not_overwrite_existing_state(tmp_path, sections):
    state = runtime(tmp_path)
    state.save_course_sections([A], 0)
    before = state.course_organization()
    with pytest.raises(ValueError):
        state.save_course_sections(sections, 1)
    assert state.course_organization() == runtime(tmp_path).course_organization() == before


def test_hierarchy_depth_limit():
    chain = [{'id': f'section-{i:032x}', 'name': f'层级 {i}',
              **({'parent_id': f'section-{i-1:032x}'} if i else {})} for i in range(7)]
    assert validate_course_sections(chain[:6]) == chain[:6]
    with pytest.raises(ValueError, match='6 层'):
        validate_course_sections(chain)


def test_failed_write_keeps_memory_and_disk_unchanged(tmp_path):
    state = runtime(tmp_path)
    state.save_course_sections([A], 0)
    before = state.course_organization()
    with patch.object(state.course_zone_store, 'save_state', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            state.save_course_sections([], 1)
        with pytest.raises(OSError):
            state.save_course_zone('1', A['id'])
    assert state.course_organization() == runtime(tmp_path).course_organization() == before


def test_stale_editor_cannot_overwrite_changes(tmp_path):
    state = runtime(tmp_path)
    state.save_course_sections([A], 0)
    with pytest.raises(ValueError):
        state.save_course_sections([B], 0)
    assert state.course_sections == [A]


@pytest.mark.parametrize('sections', [[{**A, 'name': '  '}], [A, A], [A, {**B, 'name': A['name']}], [{**A, 'id': 'archive'}], [{**A, 'id': 'unknown'}], [{**A, 'name': '归档'}]])
def test_invalid_sections_do_not_mutate(tmp_path, sections):
    state = runtime(tmp_path)
    with pytest.raises(ValueError):
        state.save_course_sections(sections, 0)
    assert state.course_sections == []


def test_api_create_move_archive_restore_and_delete(tmp_path):
    state = runtime(tmp_path)
    db = DatabaseManager(cache_dir=tmp_path / 'cache')
    app = create_app(state, db)
    routes = {(r.path, method): r.endpoint for r in app.routes if hasattr(r, 'methods') for method in r.methods}
    async def exercise():
        sections = routes[('/api/local/course-sections', 'PUT')]
        move = routes[('/api/local/course-zones', 'PUT')]
        get = routes[('/api/local/course-zones', 'GET')]
        nested = {**B, 'parent_id': A['id']}
        response = await sections(CourseSectionsRequest(sections=[A, nested], revision=0))
        assert response['sections'] == [A, nested]
        for zone in [A['id'], B['id'], 'archive', 'unassigned']:
            response = await move(CourseZoneRequest(course_id='1', zone=zone))
            assert response['zones']['1'] == zone
        with pytest.raises(HTTPException) as error:
            await move(CourseZoneRequest(course_id='1', zone='study'))
        assert error.value.status_code == 400
        with pytest.raises(HTTPException):
            await sections(CourseSectionsRequest(sections=[], revision=0))
        await sections(CourseSectionsRequest(sections=[], revision=1))
        assert (await get())['sections'] == []
    asyncio.run(exercise())
