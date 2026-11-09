"""界面冒烟测试：只验证 app.py 能正常渲染与交互，不访问 Reddit、不调大模型。

覆盖：
1. 首次渲染无异常，关键控件齐全；
2. 无凭证时点「开始分析」给出可操作的中文报错，而不是崩栈；
3. 演示模式（内置数据）能跑完采集未涉及的全链路，并自动跳到看板；
4. 看板筛选、报告页、说明页可正常切换。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")

from streamlit.testing.v1 import AppTest  # noqa: E402 - 必须在环境变量之后导入

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DASHBOARD = "② 分析看板"
DEMO = "内置示例数据（无需凭证）"


def _snapshot_reports() -> set[Path]:
    return set(REPORTS.glob("*")) if REPORTS.is_dir() else set()


class UiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._before = _snapshot_reports()
        self.at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
        self.at.run()

    def tearDown(self) -> None:
        # 只清理本次测试新生成的文件，不碰历史报告
        for path in _snapshot_reports() - self._before:
            try:
                path.unlink()
            except OSError:
                pass

    def _click_start(self) -> None:
        button = next(b for b in self.at.button if b.label == "开始分析")
        button.click().run()

    def _nav(self):
        return self.at.get("segmented_control")[0]

    def test_first_render_has_no_exception(self) -> None:
        self.assertEqual([e.value for e in self.at.exception], [])
        self.assertEqual(self._nav().label, "导航")
        self.assertEqual(self.at.radio[0].label, "运行方式")
        self.assertTrue(any(b.label == "开始分析" for b in self.at.button))
        sidebar_labels = [t.label for t in self.at.sidebar.text_input]
        for expected in ("Client ID", "Client Secret", "User-Agent", "API Key"):
            self.assertIn(expected, sidebar_labels)

    def test_live_mode_without_credentials_shows_actionable_error(self) -> None:
        self._click_start()
        self.assertEqual([e.value for e in self.at.exception], [])
        errors = " ".join(e.value for e in self.at.error)
        self.assertIn("Reddit", errors)
        self.assertIn("Client ID", errors)

    def test_demo_mode_runs_end_to_end(self) -> None:
        self.at.radio[0].set_value(DEMO).run()
        self._click_start()

        self.assertEqual([e.value for e in self.at.exception], [])
        self.assertEqual(self._nav().value, DASHBOARD)

        metrics = {m.label: m.value for m in self.at.metric}
        self.assertGreater(int(metrics["帖子样本"]), 0)
        self.assertGreater(int(metrics["功能愿望"]), 0)
        self.assertEqual(len(self.at.get("plotly_chart")), 2)
        self.assertGreaterEqual(len(self.at.dataframe), 1)

    def test_dashboard_filter_and_other_pages(self) -> None:
        self.at.radio[0].set_value(DEMO).run()
        self._click_start()

        self.at.multiselect(key="f_prio").set_value(["P0"]).run()
        self.assertEqual([e.value for e in self.at.exception], [])
        filtered = self.at.dataframe[0].value
        self.assertTrue((filtered["Priority"] == "P0").all())

        self._nav().set_value("③ 报告与导出").run()
        self.assertEqual([e.value for e in self.at.exception], [])
        downloads = [d.label for d in self.at.get("download_button")]
        self.assertIn("下载 .md", downloads)
        self.assertIn("下载 .xlsx", downloads)

        self._nav().set_value("④ 使用说明").run()
        self.assertEqual([e.value for e in self.at.exception], [])
        self.assertTrue(any("OAuth" in m.value for m in self.at.markdown))


if __name__ == "__main__":
    unittest.main()
