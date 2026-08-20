import unittest

import tokscale


class DisplayLabelTests(unittest.TestCase):
    def test_provider_and_model_labels(self) -> None:
        cases = (
            ("codex", "GPT-5.6-Sol", "gpt-5.6-sol"),
            ("openrouter", "moonshotai/kimi-k2.6", "or-moonshotai-kimi-k2.6"),
            (
                "antigravity",
                "Gemini 3.7 Flash (High)",
                "agy-gemini-3.7-flash-(high)",
            ),
            ("google", "Gemini Pro", "google-gemini-pro"),
        )
        for provider, model, expected in cases:
            with self.subTest(provider=provider, model=model):
                self.assertEqual(tokscale.display_label(provider, model), expected)


class CounterDeltaTests(unittest.TestCase):
    def test_growth_and_resets(self) -> None:
        cases = ((150, 100, 50), (25, 100, 25), (100, 100, 0), (-1, 100, 0))
        for current, previous, expected in cases:
            with self.subTest(current=current, previous=previous):
                self.assertEqual(tokscale.counter_delta(current, previous), expected)


if __name__ == "__main__":
    unittest.main()
