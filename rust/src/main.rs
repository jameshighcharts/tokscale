use chrono::{DateTime, Datelike, TimeZone, Utc};
use chrono_tz::{Europe::Oslo, Tz};
use serde::Deserialize;
use serde_json::Value;
use std::{
    collections::HashMap,
    env,
    fs::{self, File},
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{self, Command},
};

#[derive(Default, Deserialize)]
struct Record {
    #[serde(default, rename = "type")]
    kind: String,
    #[serde(default)]
    timestamp: Value,
    #[serde(default)]
    payload: Payload,
    #[serde(default)]
    message: Message,
}

#[derive(Default, Deserialize)]
struct Payload {
    #[serde(default)]
    model: String,
    #[serde(default)]
    info: Info,
}

#[derive(Default, Deserialize)]
struct Info {
    #[serde(default, rename = "last_token_usage")]
    last: Option<Usage>,
}

#[derive(Default, Deserialize)]
struct Message {
    #[serde(default)]
    model: String,
    #[serde(default)]
    usage: Option<Usage>,
}

#[derive(Default, Deserialize)]
struct PiRecord {
    #[serde(default, rename = "type")]
    kind: String,
    #[serde(default)]
    timestamp: Value,
    #[serde(default)]
    message: PiMessage,
}

#[derive(Default, Deserialize)]
struct PiMessage {
    #[serde(default)]
    role: String,
    #[serde(default)]
    provider: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    usage: Option<PiUsage>,
}

#[derive(Default, Deserialize)]
struct PiUsage {
    #[serde(default)]
    input: i64,
    #[serde(default)]
    output: i64,
    #[serde(default, rename = "cacheRead")]
    cache_read: i64,
    #[serde(default, rename = "cacheWrite")]
    cache_write: i64,
    #[serde(default, rename = "totalTokens")]
    total: i64,
    #[serde(default)]
    cost: Option<PiCost>,
}

#[derive(Default, Deserialize)]
struct PiCost {
    #[serde(default)]
    total: f64,
}

#[derive(Default, Deserialize)]
struct AntigravityRecord {
    #[serde(default)]
    captured_at: String,
    #[serde(default)]
    conversation_id: String,
    #[serde(default)]
    session_id: String,
    #[serde(default)]
    transcript_path: String,
    #[serde(default)]
    model: AntigravityModel,
    #[serde(default)]
    context_window: AntigravityContext,
}

#[derive(Default, Deserialize)]
struct AntigravityModel {
    #[serde(default)]
    id: String,
    #[serde(default)]
    display_name: String,
}

#[derive(Default, Deserialize)]
struct AntigravityContext {
    #[serde(default, rename = "total_input_tokens")]
    input: i64,
    #[serde(default, rename = "total_output_tokens")]
    output: i64,
}

#[derive(Clone, Copy, Default, Deserialize)]
struct Usage {
    #[serde(default, rename = "input_tokens")]
    input: i64,
    #[serde(default, rename = "output_tokens")]
    output: i64,
    #[serde(default, rename = "cache_read_input_tokens")]
    cache_read: i64,
    #[serde(default, rename = "cache_creation_input_tokens")]
    cache_create: i64,
    #[serde(default, rename = "cached_input_tokens")]
    cached_input: i64,
    #[serde(default, rename = "cache_write_input_tokens")]
    cache_write: i64,
    #[serde(default, rename = "total_tokens")]
    total: i64,
}

struct Event {
    provider: String,
    model: String,
    timestamp: DateTime<Utc>,
    input: i64,
    output: i64,
    read: i64,
    write: i64,
    total: i64,
    cost: Option<f64>,
}

#[derive(Default)]
struct Totals {
    model: String,
    input: i64,
    output: i64,
    read: i64,
    write: i64,
    total: i64,
    events: i64,
    cost: f64,
    cost_known: bool,
}

struct Collector {
    period: String,
    timezone: Tz,
    now: DateTime<Utc>,
    models: HashMap<String, Totals>,
    first: Option<DateTime<Utc>>,
    last: Option<DateTime<Utc>>,
}

fn display_label(provider: &str, model: &str) -> String {
    let prefix = match provider {
        "antigravity" => "agy-",
        "openrouter" => "or-",
        "pi" => "pi-",
        _ => "",
    };
    format!("{}{}", prefix, model.replace('/', "-").to_lowercase())
}

fn parse_time(value: &Value) -> DateTime<Utc> {
    if let Some(text) = value.as_str() {
        if let Ok(parsed) = DateTime::parse_from_rfc3339(text) {
            return parsed.with_timezone(&Utc);
        }
    }
    if let Some(number) = value.as_f64() {
        let millis = if number > 10_000_000_000.0 {
            number as i64
        } else {
            (number * 1000.0) as i64
        };
        if let Some(parsed) = DateTime::from_timestamp_millis(millis) {
            return parsed;
        }
    }
    Utc::now()
}

fn epoch_time(value: &str) -> DateTime<Utc> {
    let number = value.parse::<i64>().unwrap_or_default();
    if number > 10_000_000_000 {
        DateTime::from_timestamp_millis(number).unwrap_or_else(Utc::now)
    } else {
        Utc.timestamp_opt(number, 0)
            .single()
            .unwrap_or_else(Utc::now)
    }
}

fn event_cost(event: &Event) -> Option<f64> {
    if let Some(cost) = event.cost {
        return Some(cost.max(0.0));
    }
    let rate = match event.model.as_str() {
        "claude-fable-5" => Some([10.0, 50.0, 1.0, 12.5]),
        "claude-opus-5" => Some([5.0, 25.0, 0.5, 6.25]),
        "claude-sonnet-5" => Some([2.0, 10.0, 0.2, 2.5]),
        _ => None,
    };
    if let Some(rate) = rate {
        return Some(
            (event.input as f64 * rate[0]
                + event.output as f64 * rate[1]
                + event.read as f64 * rate[2]
                + event.write as f64 * rate[3])
                / 1_000_000.0,
        );
    }
    let rate = match event.model.as_str() {
        "gpt-5.6-sol" => Some(0.8),
        "codex-auto-review" => Some(0.459224),
        "gpt-5.6-luna" => Some(0.0177545),
        _ => None,
    };
    rate.map(|rate| event.total as f64 * rate / 1_000_000.0)
}

impl Collector {
    fn includes(&self, timestamp: DateTime<Utc>) -> bool {
        if self.period == "all" {
            return true;
        }
        let date = timestamp.with_timezone(&self.timezone);
        let now = self.now.with_timezone(&self.timezone);
        match self.period.as_str() {
            "daily" => date.date_naive() == now.date_naive(),
            "weekly" => date.iso_week() == now.iso_week(),
            "monthly" => date.year() == now.year() && date.month() == now.month(),
            _ => false,
        }
    }

    fn add(&mut self, event: Event) {
        if !self.includes(event.timestamp) {
            return;
        }
        let cost = event_cost(&event);
        let key = format!("{}\0{}", event.provider, event.model);
        let label = display_label(&event.provider, &event.model);
        let row = self.models.entry(key).or_insert_with(|| Totals {
            model: label,
            cost_known: true,
            ..Totals::default()
        });
        row.input += event.input;
        row.output += event.output;
        row.read += event.read;
        row.write += event.write;
        row.total += event.total;
        row.events += 1;
        if let Some(cost) = cost {
            row.cost += cost;
        } else {
            row.cost_known = false;
        }
        if self.first.map_or(true, |first| event.timestamp < first) {
            self.first = Some(event.timestamp);
        }
        if self.last.map_or(true, |last| event.timestamp > last) {
            self.last = Some(event.timestamp);
        }
    }
}

fn visit_jsonl(root: &Path, visit: &mut dyn FnMut(&Path)) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            visit_jsonl(&path, visit);
        } else if path
            .extension()
            .is_some_and(|extension| extension == "jsonl")
        {
            visit(&path);
        }
    }
}

fn scan_lines(path: &Path, relevant: fn(&str) -> bool, visit: &mut dyn FnMut(Record)) {
    let Ok(file) = File::open(path) else { return };
    for line in BufReader::new(file).lines().map_while(Result::ok) {
        if relevant(&line) {
            if let Ok(record) = serde_json::from_str(&line) {
                visit(record);
            }
        }
    }
}

fn codex_line(line: &str) -> bool {
    line.contains("\"turn_context\"") || line.contains("\"last_token_usage\"")
}

fn claude_line(line: &str) -> bool {
    line.contains("\"assistant\"") && line.contains("\"usage\"")
}

fn pi_line(line: &str) -> bool {
    line.contains("\"assistant\"") && line.contains("\"usage\"")
}

fn scan_codex(home: &Path, collector: &mut Collector) -> i64 {
    let mut found = 0;
    for root in [
        home.join(".codex/sessions"),
        home.join(".codex/archived_sessions"),
    ] {
        visit_jsonl(&root, &mut |path| {
            let mut model = String::new();
            scan_lines(path, codex_line, &mut |record| {
                if record.kind == "turn_context" && !record.payload.model.is_empty() {
                    model = record.payload.model;
                }
                let Some(usage) = record.payload.info.last else {
                    return;
                };
                if model.is_empty() {
                    return;
                }
                found += 1;
                collector.add(Event {
                    provider: "codex".to_owned(),
                    model: model.clone(),
                    timestamp: parse_time(&record.timestamp),
                    input: usage.input,
                    output: usage.output,
                    read: usage.cached_input,
                    write: usage.cache_write,
                    total: usage.total,
                    cost: None,
                });
            });
        });
    }
    found
}

fn scan_codex_sqlite(home: &Path, collector: &mut Collector) {
    let output = Command::new("sqlite3")
        .args(["-readonly", "-separator", "\t"])
        .arg(home.join(".codex/state_5.sqlite"))
        .arg("SELECT model, updated_at, tokens_used FROM threads WHERE tokens_used > 0;")
        .output();
    let Ok(output) = output else { return };
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let fields: Vec<_> = line.split('\t').collect();
        if fields.len() != 3 {
            continue;
        }
        collector.add(Event {
            provider: "codex".to_owned(),
            model: fields[0].to_owned(),
            timestamp: epoch_time(fields[1]),
            input: 0,
            output: 0,
            read: 0,
            write: 0,
            total: fields[2].parse().unwrap_or_default(),
            cost: None,
        });
    }
}

fn scan_claude(home: &Path, collector: &mut Collector) {
    visit_jsonl(&home.join(".claude/projects"), &mut |path| {
        scan_lines(path, claude_line, &mut |record| {
            let Some(usage) = record.message.usage else {
                return;
            };
            let model = record.message.model;
            if record.kind != "assistant" || model.is_empty() || model == "<synthetic>" {
                return;
            }
            collector.add(Event {
                provider: "claude".to_owned(),
                model,
                timestamp: parse_time(&record.timestamp),
                input: usage.input,
                output: usage.output,
                read: usage.cache_read,
                write: usage.cache_create,
                total: usage.input + usage.output + usage.cache_read + usage.cache_create,
                cost: None,
            });
        });
    });
}

fn scan_pi(home: &Path, collector: &mut Collector) {
    visit_jsonl(&home.join(".pi/agent/sessions"), &mut |path| {
        let Ok(file) = File::open(path) else { return };
        for line in BufReader::new(file).lines().map_while(Result::ok) {
            if !pi_line(&line) {
                continue;
            }
            let Ok(record) = serde_json::from_str::<PiRecord>(&line) else {
                continue;
            };
            let Some(usage) = record.message.usage else {
                continue;
            };
            if record.kind != "message"
                || record.message.role != "assistant"
                || record.message.model.is_empty()
            {
                continue;
            }
            collector.add(Event {
                provider: if record.message.provider.is_empty() {
                    "pi".to_owned()
                } else {
                    record.message.provider
                },
                model: record.message.model,
                timestamp: parse_time(&record.timestamp),
                input: usage.input,
                output: usage.output,
                read: usage.cache_read,
                write: usage.cache_write,
                total: usage.total,
                cost: usage.cost.map(|cost| cost.total),
            });
        }
    });
}

fn scan_antigravity(home: &Path, collector: &mut Collector) {
    let path = home.join(".gemini/antigravity-cli/tokscale-usage.jsonl");
    let Ok(file) = File::open(path) else { return };
    let mut previous: HashMap<String, (i64, i64)> = HashMap::new();
    for line in BufReader::new(file).lines().map_while(Result::ok) {
        let Ok(record) = serde_json::from_str::<AntigravityRecord>(&line) else {
            continue;
        };
        if record.captured_at.is_empty() {
            continue;
        }
        let conversation = if !record.conversation_id.is_empty() {
            record.conversation_id.clone()
        } else if !record.session_id.is_empty() {
            record.session_id.clone()
        } else {
            record.transcript_path.clone()
        };
        let model = if !record.model.id.is_empty() {
            record.model.id.clone()
        } else {
            record.model.display_name.clone()
        };
        if conversation.is_empty() || model.is_empty() {
            continue;
        }
        let old = previous.get(&conversation).copied().unwrap_or_default();
        let input = (record.context_window.input - old.0).max(0);
        let output = (record.context_window.output - old.1).max(0);
        previous.insert(
            conversation,
            (record.context_window.input, record.context_window.output),
        );
        if input == 0 && output == 0 {
            continue;
        }
        collector.add(Event {
            provider: "antigravity".to_owned(),
            model,
            timestamp: parse_time(&Value::String(record.captured_at)),
            input,
            output,
            read: 0,
            write: 0,
            total: input + output,
            cost: None,
        });
    }
}

fn token_text(value: i64) -> String {
    if value >= 1_000_000_000 {
        format!("{:.1}B", value as f64 / 1_000_000_000.0)
    } else if value >= 1_000_000 {
        format!("{:.1}M", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}K", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

fn cost_text(row: &Totals) -> String {
    if !row.cost_known {
        "—".to_owned()
    } else if row.cost >= 1000.0 {
        format!("${:.1}K", row.cost / 1000.0)
    } else if row.cost > 0.0 && row.cost < 0.01 {
        format!("${:.3}", row.cost)
    } else {
        format!("${:.2}", row.cost)
    }
}

fn interval(first: Option<DateTime<Utc>>, last: Option<DateTime<Utc>>, timezone: Tz) -> String {
    let (Some(first), Some(last)) = (first, last) else {
        return "no data".into();
    };
    let first = first.with_timezone(&timezone);
    let last = last.with_timezone(&timezone);
    let start = format!("{} {}", first.format("%b"), first.day());
    if first.date_naive() == last.date_naive() {
        start
    } else {
        format!("{} – {} {}", start, last.format("%b"), last.day())
    }
}

fn report(collector: Collector, breakdown: bool) {
    let mut rows: Vec<_> = collector.models.into_values().collect();
    rows.sort_unstable_by_key(|row| std::cmp::Reverse(row.total));
    let model_width = rows
        .iter()
        .map(|row| row.model.chars().count())
        .max()
        .unwrap_or(5)
        .max(5);
    println!(
        "tokscale · models · {}\nrange: {}\n",
        collector.period,
        interval(collector.first, collector.last, collector.timezone)
    );
    println!(
        "{:<width$} {:>12} {:>12}\n{}",
        "model",
        "tokens",
        "cost",
        "-".repeat(model_width + 1 + 12 + 1 + 12),
        width = model_width
    );
    for row in &rows {
        println!(
            "{:<width$} {:>12} {:>12}",
            row.model,
            token_text(row.total),
            cost_text(row),
            width = model_width
        );
    }
    if !breakdown {
        return;
    }
    println!(
        "\nbreakdown\n---------\n{:<width$} {:>10} {:>10} {:>12} {:>13} {:>8}\n{}",
        "model",
        "input",
        "output",
        "cache read",
        "cache write",
        "events",
        "-".repeat(model_width + 1 + 10 + 1 + 10 + 1 + 12 + 1 + 13 + 1 + 8),
        width = model_width
    );
    for row in &rows {
        println!(
            "{:<width$} {:>10} {:>10} {:>12} {:>13} {:>8}",
            row.model,
            token_text(row.input),
            token_text(row.output),
            token_text(row.read),
            token_text(row.write),
            row.events,
            width = model_width
        );
    }
    println!("\nnote: codex cache read is included inside input and is not added twice.");
}

fn arguments() -> (bool, String) {
    let mut args = env::args().skip(1);
    if args.next().as_deref() != Some("models") {
        eprintln!("usage: tokscale-rust models [--breakdown] [--daily|--weekly|--monthly|--all]");
        process::exit(2);
    }
    let mut breakdown = false;
    let mut period = "all".to_owned();
    for argument in args {
        match argument.as_str() {
            "--breakdown" => breakdown = true,
            "--daily" | "--weekly" | "--monthly" | "--all" => {
                period = argument.trim_start_matches("--").to_owned()
            }
            _ => {
                eprintln!("unknown argument: {argument}");
                process::exit(2);
            }
        }
    }
    (breakdown, period)
}

fn main() {
    let (breakdown, period) = arguments();
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let timezone = env::var("TOKSCALE_TZ")
        .ok()
        .and_then(|name| name.parse().ok())
        .unwrap_or(Oslo);
    let mut collector = Collector {
        period,
        timezone,
        now: Utc::now(),
        models: HashMap::new(),
        first: None,
        last: None,
    };
    if scan_codex(&home, &mut collector) == 0 {
        scan_codex_sqlite(&home, &mut collector);
    }
    scan_claude(&home, &mut collector);
    scan_pi(&home, &mut collector);
    scan_antigravity(&home, &mut collector);
    report(collector, breakdown);
}
