"""Bundled server and isolated course-worker entry points."""
import importlib.util
import json
import os
from pathlib import Path
import sys

RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(RESOURCE_ROOT))
os.umask(0o077)
os.environ.setdefault('ASR_MODEL_DIR', str(RESOURCE_ROOT / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17'))
os.environ.setdefault('SILERO_VAD_PATH', str(RESOURCE_ROOT / 'silero_vad.onnx'))

if '--capabilities' in sys.argv:
    modules = ['openai','sherpa_onnx','rapidocr_onnxruntime','psutil','imagehash','numpy','PIL','requests','curl_cffi','imageio_ffmpeg']
    print(json.dumps([name for name in modules if importlib.util.find_spec(name) is None]))
elif '--lecture-worker' in sys.argv:
    from scripts.run_lecture_job import worker_main
    worker_main()
elif '--worker' in sys.argv:
    from scripts.run_console_job import run
    raise SystemExit(run(json.loads(sys.stdin.readline())))
else:
    from local_web.server import main
    main()
