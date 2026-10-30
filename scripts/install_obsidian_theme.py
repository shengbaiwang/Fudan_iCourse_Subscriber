"""Install the app's palette and typography as a reversible Obsidian snippet."""

from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_web.obsidian_appearance import install, render_snippet


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path, help="现有 Obsidian 库的路径")
    args = parser.parse_args()
    print(install(args.vault))
