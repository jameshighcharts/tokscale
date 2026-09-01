package main

import "testing"

func TestDisplayLabel(t *testing.T) {
	for _, test := range []struct{ provider, model, want string }{
		{"codex", "GPT-5.6-Sol", "gpt-5.6-sol"},
		{"openrouter", "moonshotai/kimi-k2.6", "or-moonshotai-kimi-k2.6"},
		{"antigravity", "Gemini 3.7 Flash (High)", "agy-gemini-3.7-flash-(high)"},
		{"google", "Gemini Pro", "google-gemini-pro"},
	} {
		if got := displayLabel(test.provider, test.model); got != test.want {
			t.Errorf("displayLabel(%q, %q) = %q, want %q", test.provider, test.model, got, test.want)
		}
	}
}

func TestCounterDelta(t *testing.T) {
	for _, test := range []struct{ current, previous, want int64 }{
		{150, 100, 50},
		{25, 100, 25},
		{100, 100, 0},
		{-1, 100, 0},
	} {
		if got := counterDelta(test.current, test.previous); got != test.want {
			t.Errorf("counterDelta(%d, %d) = %d, want %d", test.current, test.previous, got, test.want)
		}
	}
}

func TestIncompleteTotalCost(t *testing.T) {
	row := &totals{cost: 2.5, costKnown: false}
	if got := totalCostText(row); got != "$2.50" {
		t.Fatalf("totalCostText() = %q, want %q", got, "$2.50")
	}
}
