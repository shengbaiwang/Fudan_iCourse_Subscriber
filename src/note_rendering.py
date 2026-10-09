"""Standalone course-note HTML rendering, including color and LaTeX."""

import base64
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from io import BytesIO
from urllib.parse import quote

import markdown
import requests
from PIL import Image
from pygments.formatters import HtmlFormatter


_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists", "codehilite"]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "linenums": False,
        "css_class": "highlight",
    }
}

PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".highlight")

NOTE_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f8f8f8;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[str, tuple] = {}


def _fetch_latex_image(url: str, dpi: int = 300) -> tuple:
    """Fetch rendered LaTeX image, return (width, height, png_bytes).

    Width/height are logical display pixels (DPI-adjusted).
    Returns (None, None, None) on failure.
    """
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]

    try:
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))

        result = (logical_width, logical_height, response.content)
        _IMAGE_CACHE[url] = result
        return result
    except Exception as e:
        print(f"[LaTeX Render] Image fetch failed: {e}")
        return None, None, None


def _prefetch_latex_images(urls: list[str], dpi: int = 300) -> None:
    """Pre-fetch multiple LaTeX images concurrently.

    Results are stored in ``_IMAGE_CACHE`` so that subsequent calls to
    ``_fetch_latex_image`` become instant cache hits.
    """
    uncached = [u for u in urls if u not in _IMAGE_CACHE]
    if not uncached:
        return
    with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
        futures = {pool.submit(_fetch_latex_image, u, dpi): u for u in uncached}
        for future in as_completed(futures):
            future.result()  # trigger any exception logging inside _fetch_latex_image


def markdown_to_html(md_text: str) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Formula images are embedded as data URLs so downloaded files remain
    readable offline.
    """
    latex_map: dict[str, str] = {}
    counter = 0

    def _stash(match):
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$$" + match.group(1) + "$$"
        return key

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$" + match.group(1) + "$"
        return key

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )

    # Build URL list and pre-fetch all LaTeX images concurrently
    # Each entry: (url, latex_content, is_block)
    latex_info: dict[str, tuple[str, str, bool]] = {}
    for key, original in latex_map.items():
        is_block = original.startswith("$$")
        latex_content = original[2:-2] if is_block else original[1:-1]
        prefix = r"\dpi{300}\bg{white}" if is_block else r"\dpi{300}\bg{white}\inline"
        url = f"https://latex.codecogs.com/png.latex?{prefix}%20{quote(latex_content)}"
        latex_info[key] = (url, latex_content, is_block)

    _prefetch_latex_images([info[0] for info in latex_info.values()])

    for key, (url, latex_content, is_block) in latex_info.items():
        w, h, img_data = _fetch_latex_image(url)

        if is_block:
            if w and h:
                src = _resolve_src(url, img_data)
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:middle;border:none;display:inline-block;">'
                    f'</div>'
                )
            else:
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<code>{escape(latex_content)}</code></div>'
                )
        else:
            if w and h:
                # Enforce minimum height so formulas aren't smaller than text
                if h < _MIN_INLINE_HEIGHT:
                    scale = _MIN_INLINE_HEIGHT / h
                    w = max(1, int(w * scale))
                    h = _MIN_INLINE_HEIGHT

                src = _resolve_src(url, img_data)
                img_tag = (
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:-3px;border:none;margin:0 2px;">'
                )
            else:
                img_tag = f'<code>{escape(latex_content)}</code>'

        html = html.replace(key, img_tag)

    return html


def _resolve_src(url: str, img_data: bytes | None) -> str:
    """Embed fetched formula images directly in the exported document."""
    if img_data:
        return "data:image/png;base64," + base64.b64encode(img_data).decode("ascii")
    return url
