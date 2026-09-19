import json
from pathlib import Path
from unittest.mock import patch

import pytest
import asyncio
from fastapi import HTTPException
from local_web.state import CourseZoneStore, RuntimeState, SettingsStore
from local_web.server import create_app, CourseSectionsRequest, CourseZoneRequest
from local_web.database import DatabaseManager


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


@pytest.mark.parametrize('wrapped', [False, True])
def test_legacy_migration_and_delete_all_preserves_archive(tmp_path, wrapped):
    legacy = {"1": "学习区", "2": "archive", "3": "整理区"}
    (tmp_path / 'course-zones.json').write_text(json.dumps({"zones": legacy} if wrapped else legacy))
    state = runtime(tmp_path)
    assert state.default_course_zone == 'organize'
    assert [s['id'] for s in state.course_sections] == ['organize', 'study', 'reference']
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
        response = await sections(CourseSectionsRequest(sections=[A], revision=0))
        assert response['sections'] == [A]
        for zone in [A['id'], 'archive', 'unassigned']:
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
