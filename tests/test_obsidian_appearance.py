"""Appearance synchronization only writes the opted-in snippet, with recovery."""

import json
from pathlib import Path
import shutil

import pytest

from local_web import obsidian_appearance as appearance


@pytest.fixture
def vault(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    for name in appearance.SOURCES:
        shutil.copy2(appearance.STATIC / name, static / name)
    monkeypatch.setattr(appearance, "STATIC", static)
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    return root


def test_install_preserves_preferences_backups_and_unchanged_files(vault):
    config = vault / ".obsidian"
    settings = {"cssTheme": "Existing", "theme": "system", "baseFontSize": 21,
                "textFontFamily": "User Font", "enabledCssSnippets": ["other"]}
    original = json.dumps(settings)
    (config / "appearance.json").write_text(original)
    target = appearance.install(vault)
    settings["enabledCssSnippets"].append(appearance.SNIPPET)
    assert json.loads((config / "appearance.json").read_text()) == settings
    backups = list(config.glob("sea-appearance-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "appearance.json").read_text() == original
    stamp = target.stat().st_mtime_ns
    appearance.install(vault)
    assert target.stat().st_mtime_ns == stamp
    assert len(list(config.glob("sea-appearance-backup-*"))) == 1


def test_running_sync_tracks_font_and_palette_changes_without_rewriting_settings(vault):
    target = appearance.install(vault)
    settings = vault / ".obsidian/appearance.json"
    settings_stamp = settings.stat().st_mtime_ns
    old = target.read_text()
    old_inode = target.stat().st_ino
    watcher = appearance.AppearanceSync(vault)
    watcher.refresh()
    source = appearance.STATIC / "sea.css"
    source.write_text(source.read_text().replace('"Songti SC"', '"New Serif"')
                      .replace("--paper:#fafcfd", "--paper:#f4f1e8"))
    watcher.refresh()
    assert target.stat().st_ino == old_inode
    assert '"New Serif"' in target.read_text()
    assert "--sea-paper: #f4f1e8;" in target.read_text()
    assert settings.stat().st_mtime_ns == settings_stamp
    backups = list((vault / ".obsidian").glob("sea-appearance-backup-*"))
    assert any((backup / target.name).is_file() and
               (backup / target.name).read_text() == old for backup in backups)
    target_stamp = target.stat().st_mtime_ns
    watcher.refresh()
    assert target.stat().st_mtime_ns == target_stamp
    assert len(list((vault / ".obsidian").glob("sea-appearance-backup-*"))) == len(backups)


def test_running_sync_never_installs_or_reenables_disabled_snippet(vault):
    watcher = appearance.AppearanceSync(vault)
    watcher.refresh()
    assert list((vault / ".obsidian").iterdir()) == []
    target = appearance.install(vault)
    settings = vault / ".obsidian/appearance.json"
    settings.write_text('{"enabledCssSnippets": []}')
    target.write_text("/* user's disabled snippet */")
    watcher.refresh()
    assert target.read_text() == "/* user's disabled snippet */"
    assert json.loads(settings.read_text())["enabledCssSnippets"] == []


@pytest.mark.parametrize("relative", ["appearance.json", "snippets", "snippets/sea-of-abundance.css"])
def test_symlinks_cannot_redirect_appearance_writes(vault, tmp_path, relative):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("do not change")
    target = vault / ".obsidian" / relative
    target.parent.mkdir(exist_ok=True)
    target.symlink_to(outside if relative == "snippets" else sentinel,
                      target_is_directory=relative == "snippets")
    with pytest.raises(ValueError, match="符号链接"):
        appearance.install(vault)
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text() == "do not change"
