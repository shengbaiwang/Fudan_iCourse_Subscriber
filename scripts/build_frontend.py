"""Package the canonical local console for GitHub Pages (no UI copy to maintain)."""
from pathlib import Path
import argparse
import shutil

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path) -> None:
    destination = destination.resolve()
    source = ROOT / 'local_web' / 'static'
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError('Build output must be separate from the UI source')
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    (destination / 'runtime-config.js').write_text('window.ICOURSE_RUNTIME = "pages";\n')
    (destination / '.nojekyll').touch()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'frontend')
    build(parser.parse_args().output)
