from pathlib import Path
import plistlib
from unittest.mock import patch

from fastapi.testclient import TestClient
from local_web.database import DatabaseManager
from local_web.notebook import Notebook
from local_web.server import create_app
from local_web.state import RuntimeState, SettingsStore


class NoCredentials:
    available = False
    def load(self, settings): return None


def test_desktop_session_token_and_private_notebook_api(tmp_path, monkeypatch):
    monkeypatch.setenv('SEA_DESKTOP_TOKEN', 'isolated-desktop-session')
    monkeypatch.setenv('ICOURSE_WEB_CONFIG_DIR', str(tmp_path))
    state = RuntimeState(store=SettingsStore(tmp_path), credential_store=NoCredentials())
    database = DatabaseManager(tmp_path / 'cache')
    try:
        with TestClient(create_app(state=state, database=database, notebook=Notebook(tmp_path))) as client:
            assert client.get('/').status_code == 200
            assert client.get('/api/local/notebook').status_code == 403
            headers = {'X-Sea-Session':'isolated-desktop-session'}
            created = client.post('/api/local/notebook',json={'title':'书海','body':'第一段'},headers=headers).json()
            assert created['title'] == '书海'
            saved = client.put(f"/api/local/notebook/{created['id']}",json={**created,'body':'第二段'},headers=headers)
            assert saved.status_code == 200
            conflict = client.put(f"/api/local/notebook/{created['id']}",json={**created,'body':'旧窗口'},headers=headers)
            assert conflict.status_code == 409
            archive = client.get('/api/local/notebook/backup',headers=headers)
            assert archive.status_code == 200 and archive.content.startswith(b'PK')
            restored = client.post('/api/local/notebook/restore',files={'file':('backup.zip',archive.content)},headers=headers)
            assert restored.json() == {'imported':0}
            assert client.post('/api/local/notebook/restore',files={'file':('bad.zip',b'invalid')},headers=headers).status_code == 400
            assert client.get('/api/local/notebook',headers=headers).json()[0]['body'] == '第二段'
    finally:
        database.close()


def test_application_branding_and_bundle_identity():
    root = Path(__file__).resolve().parents[1]
    metadata = plistlib.loads((root / 'desktop/Info.plist').read_bytes())
    assert metadata['CFBundleDisplayName'] == '丰饶之海'
    assert metadata['CFBundleIdentifier'] == 'app.seaofabundance.desktop'
    html = (root / 'local_web/static/index.html').read_text()
    assert '<title>丰饶之海</title>' in html
    assert 'branding/icourse-icon-192.png' in html
