from __future__ import annotations

import unittest

from src.ai.title import build_title_material, clean_title, split_generated_title


class CleanTitleTest(unittest.TestCase):
    def test_strips_prefix_quotes_and_marks(self):
        self.assertEqual(clean_title("标题：「梯度下降法」\n第二行"), "梯度下降法")

    def test_takes_only_the_first_line(self):
        self.assertEqual(clean_title("  神经\n网络   基础  "), "神经")

    def test_returns_empty_for_pure_punctuation(self):
        self.assertEqual(clean_title("《 》"), "")

    def test_returns_empty_for_empty_input(self):
        self.assertEqual(clean_title(""), "")
        self.assertEqual(clean_title(None), "")

    def test_truncates_long_titles(self):
        self.assertEqual(clean_title("长" * 50), "长" * 30)


class SplitGeneratedTitleTest(unittest.TestCase):
    def test_splits_h1_title_from_body(self):
        title, body = split_generated_title("# 梯度下降\n\n### 定义\n正文")
        self.assertEqual(title, "梯度下降")
        self.assertEqual(body, "### 定义\n正文")

    def test_cleans_the_embedded_title(self):
        title, body = split_generated_title("# 「牛顿法」  \n正文")
        self.assertEqual(title, "牛顿法")
        self.assertEqual(body, "正文")

    def test_h2_is_not_a_title(self):
        title, body = split_generated_title("## 不是标题\n正文")
        self.assertEqual(title, "")
        self.assertEqual(body, "## 不是标题\n正文")

    def test_no_title_line_keeps_original(self):
        title, body = split_generated_title("直接是正文")
        self.assertEqual(title, "")
        self.assertEqual(body, "直接是正文")

    def test_empty_title_marker_keeps_original(self):
        title, body = split_generated_title("# 《》\n正文")
        self.assertEqual(title, "")
        self.assertEqual(body, "# 《》\n正文")


class BuildTitleMaterialTest(unittest.TestCase):
    def test_course_name_alone_is_not_enough(self):
        self.assertEqual(build_title_material("数学分析", "", []), "")

    def test_ppt_leads_and_transcript_follows(self):
        material = build_title_material(
            "数学分析",
            "今天我们讲导数",
            [{"text": "第 3 章 导数与微分"}, {"text": "定义 3.1 导数"}],
        )
        self.assertIn("课程：数学分析", material)
        self.assertIn("【PPT 课件文字】", material)
        self.assertIn("第 3 章 导数与微分", material)
        self.assertLess(material.index("【PPT 课件文字】"), material.index("【录音转录（节选）】"))

    def test_skips_blank_pages(self):
        material = build_title_material(
            "", "转录内容", [{"text": "  "}, {"text": "有效页"}]
        )
        self.assertIn("有效页", material)
        self.assertNotIn("\n\n\n", material)

    def test_truncates_sources_to_their_budgets(self):
        material = build_title_material(
            "", "转" * 5000, [{"text": "页" * 5000}], ppt_limit=100, transcript_limit=50
        )
        self.assertIn("页" * 100, material)
        self.assertNotIn("页" * 101, material)
        self.assertIn("转" * 50, material)
        self.assertNotIn("转" * 51, material)


if __name__ == "__main__":
    unittest.main()
