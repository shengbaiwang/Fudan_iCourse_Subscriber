from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
model = 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17'
data = [(str(root / 'local_web/static'), 'local_web/static'),
        (str(root / 'silero_vad.onnx'), '.'), (str(root / model), model)]
for package in ['rapidocr_onnxruntime', 'imageio_ffmpeg', 'certifi']:
    data += collect_data_files(package)
hidden = ['main', 'scripts.run_console_job', 'scripts.resummarize',
          'scripts.generate_titles', 'scripts.local_course', 'scripts.local_pipeline',
          'scripts.local_audio', 'uvicorn.logging', 'uvicorn.loops.auto',
          'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
          'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
          'sherpa_onnx', 'rapidocr_onnxruntime', 'imageio_ffmpeg']
hidden += collect_submodules('sherpa_onnx')
a = Analysis([str(root / 'desktop/backend.py')], pathex=[str(root)],
             binaries=[], datas=data, hiddenimports=hidden,
             excludes=['pytest', 'tkinter', 'matplotlib', 'IPython'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='sea-backend',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, target_arch='arm64')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='sea-backend')
