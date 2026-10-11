"""Run the native Dev app against this checkout, without packaging Python."""
import argparse
import fcntl
import hashlib
import platform
import plistlib
from pathlib import Path
import shutil
import sys

from build_desktop import ROOT, build_icon, run


def build():
    if sys.platform != 'darwin':
        raise SystemExit('丰饶之海 Dev 需要 macOS。')
    output = ROOT / 'dist/desktop-dev'
    work = output / 'build'
    app = output / '丰饶之海 Dev.app'
    work.mkdir(parents=True, exist_ok=True)
    metadata = plistlib.loads((ROOT / 'desktop/Info.plist').read_bytes())
    metadata.update(
        CFBundleName='丰饶之海 Dev',
        CFBundleDisplayName='丰饶之海 Dev',
        CFBundleIdentifier='app.seaofabundance.desktop.dev',
        SeaDevelopment={'ProjectRoot': str(ROOT), 'PythonExecutable': sys.executable},
    )
    plist = plistlib.dumps(metadata)
    native_source = ROOT / 'desktop/SeaOfAbundance.swift'
    icon_source = ROOT / 'local_web/static/branding/icourse-icon-1024.png'
    target = f'{platform.machine()}-apple-macosx13.0'
    signature = hashlib.sha256(plist + native_source.read_bytes() + icon_source.read_bytes()
                               + Path(__file__).read_bytes() + target.encode()).hexdigest()
    stamp = work / 'signature'
    executable = app / 'Contents/MacOS/SeaOfAbundance'
    if executable.is_file() and stamp.is_file() and stamp.read_text() == signature:
        print('Dev 外壳已是最新，直接读取项目源码。', flush=True)
        return app

    # Build beside the current app so a compiler error leaves the last build usable.
    staging = output / 'staging/丰饶之海 Dev.app'
    if staging.exists():
        shutil.rmtree(staging)
    contents = staging / 'Contents'
    macos, resources = contents / 'MacOS', contents / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir()
    (contents / 'Info.plist').write_bytes(plist)
    print('编译 Dev 原生外壳（Python、前端和模型无需打包）…', flush=True)
    run('swiftc', '-module-cache-path', work / 'swift-cache', '-Onone', '-g',
        '-target', target, native_source, '-o', macos / 'SeaOfAbundance',
        '-framework', 'AppKit', '-framework', 'WebKit')
    icon = work / 'Sea.icns'
    icon_stamp = work / 'icon-signature'
    icon_signature = hashlib.sha256(icon_source.read_bytes()).hexdigest()
    if not icon.is_file() or not icon_stamp.is_file() or icon_stamp.read_text() != icon_signature:
        build_icon(work, work)
        icon_stamp.write_text(icon_signature)
    shutil.copy2(icon, resources / 'Sea.icns')
    run('codesign', '--force', '--deep', '--sign', '-', staging)
    run('codesign', '--verify', '--deep', '--strict', staging)
    if app.exists():
        shutil.rmtree(app)
    staging.rename(app)
    stamp.write_text(signature)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-open', action='store_true', help='仅准备 Dev 外壳，不打开窗口')
    args = parser.parse_args()
    if args.no_open:
        print(f'Dev: {build()}', flush=True)
    else:
        output = ROOT / 'dist/desktop-dev'
        output.mkdir(parents=True, exist_ok=True)
        with (output / 'dev.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Dev 已在运行；Python 或 Swift 修改后请先用 ⌘Q 退出，再启动。', flush=True)
                app = output / '丰饶之海 Dev.app'
                if app.exists():
                    run('open', app)
            else:
                app = build()
                print(f'Dev: {app}', flush=True)
                # Keep the terminal's development environment and permissions.
                # The parent holds the instance lock until the native window exits.
                run(app / 'Contents/MacOS/SeaOfAbundance')
