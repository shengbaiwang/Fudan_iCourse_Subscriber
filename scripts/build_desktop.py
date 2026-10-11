"""Build a self-contained macOS app and a portable ZIP in dist/desktop."""
from pathlib import Path
import argparse
import os
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True, cwd=ROOT)


def build_icon(resources, work):
    iconset = work / 'Sea.iconset'
    iconset.mkdir(exist_ok=True)
    source = ROOT / 'local_web/static/branding/icourse-icon-1024.png'
    for size in [16, 32, 128, 256, 512]:
        for scale in [1, 2]:
            suffix = '@2x' if scale == 2 else ''
            run('sips', '-z', str(size * scale), str(size * scale), source,
                '--out', iconset / f'icon_{size}x{size}{suffix}.png')
    run('iconutil', '-c', 'icns', iconset, '-o', resources / 'Sea.icns')


def build(skip_backend=False):
    output = ROOT / 'dist' / 'desktop'
    work = ROOT / 'dist' / 'desktop-build'
    app = output / '丰饶之海.app'
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if not skip_backend:
        environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / 'pyinstaller-cache'))
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--log-level', 'WARN',
                        '--distpath', str(work / 'frozen'), '--workpath', str(work / 'analysis'),
                        str(ROOT / 'desktop/sea.spec')], cwd=ROOT, env=environment, check=True)
    backend = work / 'frozen' / 'sea-backend'
    if not backend.is_dir():
        raise SystemExit('Backend bundle is missing. Run without --skip-backend first.')
    if app.exists():
        shutil.rmtree(app)
    contents = app / 'Contents'
    macos, resources = contents / 'MacOS', contents / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(backend, resources / 'backend')
    static = resources / 'backend' / '_internal' / 'local_web' / 'static'
    shutil.rmtree(static, ignore_errors=True)
    shutil.copytree(ROOT / 'local_web/static', static)
    shutil.copy2(ROOT / 'desktop/Info.plist', contents / 'Info.plist')
    run('swiftc', '-module-cache-path', str(work / 'swift-cache'), '-O',
        '-target', 'arm64-apple-macosx13.0', ROOT / 'desktop/SeaOfAbundance.swift',
        '-o', macos / 'SeaOfAbundance', '-framework', 'AppKit', '-framework', 'WebKit')
    build_icon(resources, work)
    run('codesign', '--force', '--deep', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)
    archive = output / '丰饶之海-macOS-arm64.zip'
    if archive.exists():
        archive.unlink()
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive)
    print(f'App: {app}\nZIP: {archive}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-backend', action='store_true', help='Reuse the last frozen backend after native-only changes')
    build(parser.parse_args().skip_backend)
