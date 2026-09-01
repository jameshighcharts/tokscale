import io
import unittest
from unittest.mock import patch

import tokscale


class TokscaleTests(unittest.TestCase):
    def test_provider_and_model_labels(self) -> None:
        for provider, model, expected in (
            ("codex", "GPT-5.6-Sol", "gpt-5.6-sol"),
            ("openrouter", "moonshotai/kimi-k2.6", "or-moonshotai-kimi-k2.6"),
            ("antigravity", "Gemini 3.7 Flash (High)", "agy-gemini-3.7-flash-(high)"),
            ("google", "Gemini Pro", "google-gemini-pro"),
        ):
            with self.subTest(provider=provider, model=model):
                self.assertEqual(tokscale.display_label(provider, model), expected)

    def test_growth_and_resets(self) -> None:
        self.assertEqual(
            [
                tokscale.counter_delta(*values)
                for values in ((150, 100), (25, 100), (100, 100), (-1, 100))
            ],
            [50, 25, 0, 0],
        )

    def test_models_table_ends_with_total(self) -> None:
        models = [
            {"label": "known", "tokens": 1_500_000, "cost": 2.5, "cost_known": True},
            {"label": "unknown", "tokens": 500_000, "cost": 0.0, "cost_known": False},
        ]
        output = io.StringIO()
        with patch.object(tokscale, "collect", return_value=(models, None, None)):
            with patch("sys.stdout", output):
                tokscale.print_models(False, "all")

        self.assertEqual(output.getvalue().splitlines()[-1], "total           2.0M       $2.50+")


if __name__ == "__main__":
    unittest.main()
