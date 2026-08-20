package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
)

type usage struct {
	Input       int64 `json:"input_tokens"`
	Output      int64 `json:"output_tokens"`
	CacheRead   int64 `json:"cache_read_input_tokens"`
	CacheCreate int64 `json:"cache_creation_input_tokens"`
	CachedInput int64 `json:"cached_input_tokens"`
	CacheWrite  int64 `json:"cache_write_input_tokens"`
	Total       int64 `json:"total_tokens"`
}

type record struct {
	Type      string `json:"type"`
	Timestamp any    `json:"timestamp"`
	Payload   struct {
		Model string `json:"model"`
		Info  struct {
			Last *usage `json:"last_token_usage"`
		} `json:"info"`
	} `json:"payload"`
	Message struct {
		Model string `json:"model"`
		Usage *usage `json:"usage"`
	} `json:"message"`
}

type piRecord struct {
	Type      string `json:"type"`
	Timestamp any    `json:"timestamp"`
	Message   struct {
		Role     string   `json:"role"`
		Provider string   `json:"provider"`
		Model    string   `json:"model"`
		Usage    *piUsage `json:"usage"`
	} `json:"message"`
}

type piUsage struct {
	Input      int64   `json:"input"`
	Output     int64   `json:"output"`
	CacheRead  int64   `json:"cacheRead"`
	CacheWrite int64   `json:"cacheWrite"`
	Total      int64   `json:"totalTokens"`
	Cost       *piCost `json:"cost"`
}

type piCost struct {
	Total *float64 `json:"total"`
}

type antigravityRecord struct {
	CapturedAt     string `json:"captured_at"`
	ConversationID string `json:"conversation_id"`
	SessionID      string `json:"session_id"`
	TranscriptPath string `json:"transcript_path"`
	Model          struct {
		ID          string `json:"id"`
		DisplayName string `json:"display_name"`
	} `json:"model"`
	ContextWindow struct {
		Input  int64 `json:"total_input_tokens"`
		Output int64 `json:"total_output_tokens"`
	} `json:"context_window"`
}

type antigravitySnapshot struct {
	input, output int64
}

type event struct {
	provider                   string
	model                      string
	timestamp                  time.Time
	input, output, read, write int64
	total                      int64
	cost                       float64
	hasCost                    bool
}

type totals struct {
	model                      string
	input, output, read, write int64
	total, events              int64
	cost                       float64
	costKnown                  bool
}

type modelKey struct {
	provider, model string
}

type collector struct {
	period      string
	tz          *time.Location
	now         time.Time
	models      map[modelKey]*totals
	first, last time.Time
}

var claudeRates = map[string][4]float64{
	"claude-fable-5":  {10, 50, 1, 12.5},
	"claude-opus-5":   {5, 25, .5, 6.25},
	"claude-sonnet-5": {2, 10, .2, 2.5},
}

var openAIRates = map[string]float64{
	"gpt-5.6-sol":       .8,
	"codex-auto-review": .459224,
	"gpt-5.6-luna":      .0177545,
}

// API-equivalent proxy rates for Antigravity's Gemini models, USD per million
// tokens. Gemini 3.6/3.7 introductory standard pricing applies through 2026.
var geminiProxyRates = map[string][3]float64{
	"gemini-3.7-flash": {.75, 3.75, .075},
	"gemini-3.6-flash": {.75, 3.75, .075},
	"gemini-3.5-flash": {1.5, 9, .15},
}

func location() *time.Location {
	name := os.Getenv("TOKSCALE_TZ")
	if name == "" {
		name = "Europe/Oslo"
	}
	tz, err := time.LoadLocation(name)
	if err != nil {
		return time.Local
	}
	return tz
}

func parseTime(value any, tz *time.Location) time.Time {
	switch value := value.(type) {
	case string:
		if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
			return parsed.In(tz)
		}
	case float64:
		seconds, nanos := int64(value), int64(0)
		if value > 10_000_000_000 {
			seconds, nanos = int64(value/1000), int64(value)%1000*1_000_000
		}
		return time.Unix(seconds, nanos).In(tz)
	}
	return time.Now().In(tz)
}

func slug(value string) string {
	return strings.Map(func(character rune) rune {
		if unicode.IsSpace(character) || character == '/' {
			return '-'
		}
		return unicode.ToLower(character)
	}, value)
}

func displayLabel(provider, model string) string {
	prefix := ""
	switch provider {
	case "codex", "claude":
	case "antigravity":
		prefix = "agy"
	case "openrouter":
		prefix = "or"
	default:
		prefix = slug(provider)
	}
	model = slug(model)
	if prefix == "" {
		return model
	}
	return prefix + "-" + model
}

func counterDelta(current, previous int64) int64 {
	if current < 0 {
		current = 0
	}
	if current < previous {
		return current
	}
	return current - previous
}

func (c *collector) includes(t time.Time) bool {
	if c.period == "all" {
		return true
	}
	now := c.now
	t = t.In(c.tz)
	switch c.period {
	case "daily":
		y1, m1, d1 := t.Date()
		y2, m2, d2 := now.Date()
		return y1 == y2 && m1 == m2 && d1 == d2
	case "weekly":
		y1, w1 := t.ISOWeek()
		y2, w2 := now.ISOWeek()
		return y1 == y2 && w1 == w2
	case "monthly":
		y1, m1, _ := t.Date()
		y2, m2, _ := now.Date()
		return y1 == y2 && m1 == m2
	}
	return false
}

func eventCost(e event) (float64, bool) {
	if e.hasCost {
		if e.cost < 0 {
			return 0, true
		}
		return e.cost, true
	}
	if e.provider == "antigravity" {
		model := strings.ReplaceAll(strings.ToLower(e.model), " ", "-")
		for name, rate := range geminiProxyRates {
			if strings.Contains(model, name) {
				return (float64(e.input)*rate[0] + float64(e.output)*rate[1] +
					float64(e.read)*rate[2]) / 1e6, true
			}
		}
	}
	if rate, ok := claudeRates[e.model]; ok {
		return (float64(e.input)*rate[0] + float64(e.output)*rate[1] +
			float64(e.read)*rate[2] + float64(e.write)*rate[3]) / 1e6, true
	}
	if rate, ok := openAIRates[e.model]; ok {
		return float64(e.total) * rate / 1e6, true
	}
	return 0, false
}

func (c *collector) add(e event) {
	if !c.includes(e.timestamp) {
		return
	}
	key := modelKey{provider: e.provider, model: e.model}
	row := c.models[key]
	if row == nil {
		row = &totals{model: displayLabel(e.provider, e.model), costKnown: true}
		c.models[key] = row
	}
	row.input += e.input
	row.output += e.output
	row.read += e.read
	row.write += e.write
	row.total += e.total
	row.events++
	if cost, ok := eventCost(e); ok {
		row.cost += cost
	} else {
		row.costKnown = false
	}
	if c.first.IsZero() || e.timestamp.Before(c.first) {
		c.first = e.timestamp
	}
	if c.last.IsZero() || e.timestamp.After(c.last) {
		c.last = e.timestamp
	}
}

func scanLines(path string, relevant func([]byte) bool, visit func(record)) {
	file, err := os.Open(path)
	if err != nil {
		return
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		if !relevant(scanner.Bytes()) {
			continue
		}
		var item record
		if json.Unmarshal(scanner.Bytes(), &item) == nil {
			visit(item)
		}
	}
}

func codexLine(line []byte) bool {
	return bytes.Contains(line, []byte(`"turn_context"`)) ||
		bytes.Contains(line, []byte(`"last_token_usage"`))
}

func usageLine(line []byte) bool {
	return bytes.Contains(line, []byte(`"assistant"`)) &&
		bytes.Contains(line, []byte(`"usage"`))
}

func walkJSONL(root string, visit func(string)) {
	_ = filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && strings.HasSuffix(path, ".jsonl") {
			visit(path)
		}
		return nil
	})
}

func scanCodex(home string, c *collector) int64 {
	var found int64
	for _, root := range []string{
		filepath.Join(home, ".codex", "sessions"),
		filepath.Join(home, ".codex", "archived_sessions"),
	} {
		walkJSONL(root, func(path string) {
			model := ""
			scanLines(path, codexLine, func(item record) {
				if item.Type == "turn_context" && item.Payload.Model != "" {
					model = item.Payload.Model
				}
				if model == "" || item.Payload.Info.Last == nil {
					return
				}
				u := item.Payload.Info.Last
				found++
				c.add(event{provider: "codex", model: model, timestamp: parseTime(item.Timestamp, c.tz),
					input: u.Input, output: u.Output, read: u.CachedInput, write: u.CacheWrite, total: u.Total})
			})
		})
	}
	return found
}

func scanCodexSQLite(home string, c *collector) {
	database := filepath.Join(home, ".codex", "state_5.sqlite")
	query := "SELECT model, updated_at, tokens_used FROM threads WHERE tokens_used > 0;"
	output, err := exec.Command("sqlite3", "-readonly", "-separator", "\t", database, query).Output()
	if err != nil {
		return
	}
	for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
		parts := strings.Split(line, "\t")
		if len(parts) != 3 {
			continue
		}
		timestamp, _ := strconv.ParseFloat(parts[1], 64)
		total, _ := strconv.ParseInt(parts[2], 10, 64)
		c.add(event{provider: "codex", model: parts[0], timestamp: parseTime(timestamp, c.tz), total: total})
	}
}

func scanClaude(home string, c *collector) {
	walkJSONL(filepath.Join(home, ".claude", "projects"), func(path string) {
		scanLines(path, usageLine, func(item record) {
			u, model := item.Message.Usage, item.Message.Model
			if item.Type != "assistant" || u == nil || model == "" || model == "<synthetic>" {
				return
			}
			c.add(event{provider: "claude", model: model, timestamp: parseTime(item.Timestamp, c.tz),
				input: u.Input, output: u.Output, read: u.CacheRead, write: u.CacheCreate,
				total: u.Input + u.Output + u.CacheRead + u.CacheCreate})
		})
	})
}

func scanPi(home string, c *collector) {
	walkJSONL(filepath.Join(home, ".pi", "agent", "sessions"), func(path string) {
		file, err := os.Open(path)
		if err != nil {
			return
		}
		defer file.Close()
		scanner := bufio.NewScanner(file)
		scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
		for scanner.Scan() {
			if !usageLine(scanner.Bytes()) {
				continue
			}
			var item piRecord
			if json.Unmarshal(scanner.Bytes(), &item) != nil || item.Type != "message" ||
				item.Message.Role != "assistant" || item.Message.Model == "" || item.Message.Usage == nil {
				continue
			}
			u := item.Message.Usage
			provider := item.Message.Provider
			if provider == "" {
				provider = "pi"
			}
			cost, hasCost := 0.0, false
			if u.Cost != nil && u.Cost.Total != nil {
				cost, hasCost = *u.Cost.Total, true
			}
			c.add(event{provider: provider, model: item.Message.Model, timestamp: parseTime(item.Timestamp, c.tz),
				input: u.Input, output: u.Output, read: u.CacheRead, write: u.CacheWrite, total: u.Total,
				cost: cost, hasCost: hasCost})
		}
	})
}

func scanAntigravity(home string, c *collector) {
	path := filepath.Join(home, ".gemini", "antigravity-cli", "tokscale-usage.jsonl")
	file, err := os.Open(path)
	if err != nil {
		return
	}
	defer file.Close()
	previous := make(map[string]antigravitySnapshot)
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		var item antigravityRecord
		if json.Unmarshal(scanner.Bytes(), &item) != nil || item.CapturedAt == "" {
			continue
		}
		conversation := item.ConversationID
		if conversation == "" {
			conversation = item.SessionID
		}
		if conversation == "" {
			conversation = item.TranscriptPath
		}
		model := item.Model.ID
		if model == "" {
			model = item.Model.DisplayName
		}
		if conversation == "" || model == "" {
			continue
		}
		old := previous[conversation]
		input := max(item.ContextWindow.Input, 0)
		output := max(item.ContextWindow.Output, 0)
		deltaInput := counterDelta(input, old.input)
		deltaOutput := counterDelta(output, old.output)
		previous[conversation] = antigravitySnapshot{input, output}
		if deltaInput == 0 && deltaOutput == 0 {
			continue
		}
		c.add(event{provider: "antigravity", model: model, timestamp: parseTime(item.CapturedAt, c.tz),
			input: deltaInput, output: deltaOutput, total: deltaInput + deltaOutput})
	}
}

func tokenText(value int64) string {
	switch {
	case value >= 1e9:
		return fmt.Sprintf("%.1fB", float64(value)/1e9)
	case value >= 1e6:
		return fmt.Sprintf("%.1fM", float64(value)/1e6)
	case value >= 1e3:
		return fmt.Sprintf("%.1fK", float64(value)/1e3)
	default:
		return strconv.FormatInt(value, 10)
	}
}

func costText(row *totals) string {
	if !row.costKnown {
		return "—"
	}
	if row.cost >= 1000 {
		return fmt.Sprintf("$%.1fK", row.cost/1000)
	}
	if row.cost > 0 && row.cost < .01 {
		return fmt.Sprintf("$%.3f", row.cost)
	}
	return fmt.Sprintf("$%.2f", row.cost)
}

func interval(first, last time.Time) string {
	if first.IsZero() || last.IsZero() {
		return "no data"
	}
	start, end := fmt.Sprintf("%s %d", first.Format("Jan"), first.Day()),
		fmt.Sprintf("%s %d", last.Format("Jan"), last.Day())
	if first.Format("2006-01-02") == last.Format("2006-01-02") {
		return start
	}
	return start + " – " + end
}

func report(c *collector, breakdown bool) {
	rows := make([]*totals, 0, len(c.models))
	for _, row := range c.models {
		rows = append(rows, row)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].total > rows[j].total })
	modelWidth := len("model")
	for _, row := range rows {
		if width := len([]rune(row.model)); width > modelWidth {
			modelWidth = width
		}
	}
	fmt.Printf("tokscale · models · %s\nrange: %s\n\n", c.period, interval(c.first, c.last))
	fmt.Printf("%-*s %12s %12s\n%s\n", modelWidth, "model", "tokens", "cost",
		strings.Repeat("-", modelWidth+26))
	for _, row := range rows {
		fmt.Printf("%-*s %12s %12s\n", modelWidth, row.model, tokenText(row.total), costText(row))
	}
	if !breakdown {
		return
	}
	fmt.Printf("\nbreakdown\n---------\n%-*s %10s %10s %12s %13s %8s\n%s\n",
		modelWidth, "model", "input", "output", "cache read", "cache write", "events",
		strings.Repeat("-", modelWidth+58))
	for _, row := range rows {
		fmt.Printf("%-*s %10s %10s %12s %13s %8d\n", modelWidth, row.model, tokenText(row.input),
			tokenText(row.output), tokenText(row.read), tokenText(row.write), row.events)
	}
	fmt.Println("\nnote: codex cache read is included inside input and is not added twice.")
}

func arguments() (bool, string) {
	args := os.Args[1:]
	if len(args) == 0 || args[0] != "models" {
		fmt.Fprintln(os.Stderr, "usage: tokscale-go models [--breakdown] [--daily|--weekly|--monthly|--all]")
		os.Exit(2)
	}
	breakdown, period := false, "all"
	for _, arg := range args[1:] {
		switch arg {
		case "--breakdown":
			breakdown = true
		case "--daily", "--weekly", "--monthly", "--all":
			period = strings.TrimPrefix(arg, "--")
		default:
			fmt.Fprintf(os.Stderr, "unknown argument: %s\n", arg)
			os.Exit(2)
		}
	}
	return breakdown, period
}

func main() {
	breakdown, period := arguments()
	home, err := os.UserHomeDir()
	if err != nil {
		panic(err)
	}
	tz := location()
	c := &collector{period: period, tz: tz, now: time.Now().In(tz), models: make(map[modelKey]*totals)}
	if scanCodex(home, c) == 0 {
		scanCodexSQLite(home, c)
	}
	scanClaude(home, c)
	scanPi(home, c)
	scanAntigravity(home, c)
	report(c, breakdown)
}
