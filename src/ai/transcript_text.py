"""Conservative, content-preserving cleanup for ASR output.

This module deliberately does *not* remove filler words or characters from
any human language.  Transcript cleanup is irreversible once it is persisted,
so the ASR layer may only remove tokens that are known to be backend control
markers and normalize horizontal whitespace.

Readability transformations (filler removal, punctuation rewriting, term
correction) belong in a separate derived view and must never overwrite the
source transcript.
"""

from __future__ import annotations

import re


# FireRed emits the literal token ``<sil>``.  SenseVoice can expose control
# tokens in the form ``<|zh|>``, ``<|HAPPY|>`` or ``<|Speech|>`` depending on
# the sherpa-onnx version.  Keep this pattern intentionally narrow: a generic
# ``<...>`` expression would also delete legitimate material such as
# ``vector<int>`` from a programming lecture.
# Explicit allow-list from the supported FireRed/SenseVoice output formats.
# Unknown ``<|...|>`` strings are retained: a little visible metadata is much
# safer than silently deleting a newly introduced token that might be content.
_ASR_CONTROL_TOKEN_RE = re.compile(
    r"<sil>|<\|(?:"
    r"zh|en|yue|ja|ko|nospeech|"
    r"happy|sad|angry|neutral|emo_unknown|"
    r"speech|bgm|applause|laughter|cry|cough|sneeze|breath|noise|"
    r"withitn|withoutitn|woitn"
    r")\|>",
    re.IGNORECASE,
)
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t\u3000]+")


def sanitize_asr_segment(text: str) -> str:
    """Remove backend metadata without deleting spoken content.

    The operation is deterministic and idempotent.  In particular, it keeps
    fillers, punctuation, English function words, kana and Hangul verbatim.
    """
    if not text:
        return ""
    text = _ASR_CONTROL_TOKEN_RE.sub("", text)
    text = _HORIZONTAL_WHITESPACE_RE.sub(" ", text)
    return text.strip()
