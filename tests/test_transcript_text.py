from __future__ import annotations

import unittest

from src.ai.transcript_text import sanitize_asr_segment


class ConservativeTranscriptCleanupTest(unittest.TestCase):
    def test_preserves_english_function_words_and_technical_text(self):
        text = "The history of science is actually a vector<int> topic."
        self.assertEqual(sanitize_asr_segment(text), text)

    def test_preserves_fillers_because_they_can_carry_discourse_meaning(self):
        text = "嗯，啊，这个结论 really only applies to that case, okay?"
        self.assertEqual(sanitize_asr_segment(text), text)

    def test_preserves_multilingual_course_content(self):
        text = "《道藏提要》；日本語の資料；한국어 문헌；粤语例句。"
        self.assertEqual(sanitize_asr_segment(text), text)

    def test_preserves_punctuation_exactly(self):
        text = "真的吗？！A...B；C，D。"
        self.assertEqual(sanitize_asr_segment(text), text)

    def test_removes_only_known_backend_control_tokens(self):
        text = "<|zh|><|Speech|> 课程内容 <sil> <|HAPPY|>结束"
        self.assertEqual(sanitize_asr_segment(text), "课程内容 结束")

    def test_does_not_treat_arbitrary_angle_brackets_as_metadata(self):
        text = "vector<int>、a < b、HTML <section>、<|COURSE_TERM|> 都应保留"
        self.assertEqual(sanitize_asr_segment(text), text)

    def test_is_idempotent(self):
        text = "<|zh|>  The\t history　of science。"
        once = sanitize_asr_segment(text)
        self.assertEqual(sanitize_asr_segment(once), once)


if __name__ == "__main__":
    unittest.main()
