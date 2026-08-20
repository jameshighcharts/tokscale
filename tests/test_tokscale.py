import unittest

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


if __name__ == "__main__":
    unittest.main()
