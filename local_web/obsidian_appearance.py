"""Keep an enabled Obsidian snippet aligned with the app's own CSS tokens."""

from __future__ import annotations

import colorsys
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile


STATIC = Path(__file__).with_name("static")
SNIPPET = "sea-of-abundance"
SOURCES = ("styles.css", "sea.css", "obsidian.css")
TYPOGRAPHY = {"serif", "sans", "mono", "reading-font-size", "reading-line-height",
              "reading-letter-spacing", "reading-weight", "emphasis-weight"}


def render_snippet() -> str:
    palettes: dict[str, dict[str, str]] = {"light": {}, "dark": {}}
    # Preserve the app's CSS cascade, including later inline-code overrides.
    for name in SOURCES[:2]:
        css = (STATIC / name).read_text("utf-8")
        for match in re.finditer(r'(:root|html\[data-theme="dark"\])\s*\{([^{}]*)\}', css):
            theme = "light" if match[1] == ":root" else "dark"
            palettes[theme].update(re.findall(r"--([\w-]+)\s*:\s*([^;{}]+)", match[2]))
    palettes["dark"] = {**palettes["light"], **palettes["dark"]}
    blocks = ["/* 丰饶之海 · generated from the app palette and typography.\n"
              "   Disable sea-of-abundance in Obsidian → Appearance → CSS snippets to revert. */\n"]
    for theme, values in palettes.items():
        exported = {key: value.strip() for key, value in values.items()
                    if key in TYPOGRAPHY or re.fullmatch(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)", value.strip())}
        r, g, b = (int(exported["blue"][i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        exported.update({"accent-h": f"{h * 360:.4f}", "accent-s": f"{s * 100:.4f}%",
                         "accent-l": f"{l * 100:.4f}%"})
        declarations = "\n".join(f"  --sea-{key}: {value};" for key, value in exported.items())
        blocks.append(f"body.theme-{theme} {{\n{declarations}\n}}\n")
    blocks.append((STATIC / "obsidian.css").read_text("utf-8"))
    return "\n".join(blocks)


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install(vault: Path, *, enable: bool = True) -> Path | None:
    vault = vault.expanduser().absolute()
    if vault.is_symlink():
        raise ValueError("Obsidian 库不能是符号链接")
    config = vault.resolve(strict=True) / ".obsidian"
    if not config.is_dir() or config.is_symlink():
        raise ValueError("目标必须是已有 Obsidian 库，且 .obsidian 不能是符号链接")
    appearance = config / "appearance.json"
    snippets = config / "snippets"
    target = snippets / f"{SNIPPET}.css"
    if any(path.is_symlink() for path in (appearance, snippets, target)):
        raise ValueError("外观配置和配色片段不能是符号链接")
    settings = json.loads(appearance.read_text("utf-8")) if appearance.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("appearance.json 必须是 JSON 对象")
    enabled = settings.get("enabledCssSnippets", [])
    if not isinstance(enabled, list):
        raise ValueError("enabledCssSnippets 必须是列表")
    # Background refresh honors the user's snippet switch and never opts in a vault.
    if not enable and (SNIPPET not in enabled or not target.is_file()):
        return None
    content = render_snippet()
    if target.exists() and target.read_text("utf-8") == content and SNIPPET in enabled:
        return target
    backup = config / f"sea-appearance-backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
    backup.mkdir()
    for source in (appearance, target):
        if source.is_file():
            shutil.copy2(source, backup / source.name)
    snippets.mkdir(exist_ok=True)
    # Keep the existing inode: Obsidian caches snippets and its file watcher can
    # miss invalidation when a hidden temporary file is renamed over the target.
    # The exact previous contents are already backed up above.
    target.write_text(content, "utf-8")
    if SNIPPET not in enabled:
        settings["enabledCssSnippets"] = [*enabled, SNIPPET]
        _atomic_write(appearance, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return target


class AppearanceSync:
    """Refresh only when source/settings/snippet files change, without note writes."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.signature = None

    def _signature(self):
        config = self.vault / ".obsidian"
        paths = [*(STATIC / name for name in SOURCES), config / "appearance.json",
                 config / "snippets" / f"{SNIPPET}.css"]
        return tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
                     for path in paths)

    def refresh(self) -> None:
        try:
            signature = self._signature()
            if signature == self.signature:
                return
            self.signature = signature
            if (self.vault / ".obsidian").is_dir():
                install(self.vault, enable=False)
            self.signature = self._signature()
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Obsidian 外观同步未完成", exc_info=True)
