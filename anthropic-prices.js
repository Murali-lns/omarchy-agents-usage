.pragma library

// Anthropic API list prices, read by the API equiv. card on the Claude Code
// tab. Static data: nothing here is fetched or read from disk at runtime.
// Update `checkedAt` with the table whenever the source page changes.
var source = "https://platform.claude.com/docs/en/about-claude/pricing"
var checkedAt = "2026-11-24"

// USD per million tokens, keyed by API model id without a date suffix.
// cacheWrite5m and cacheWrite1h are the 5-minute and 1-hour cache-write
// prices; usage records do not say which one a write used.
// Claude Haiku 5.5 bills prompts over 100,000 tokens at higher prices; usage
// records do not keep prompt lengths, so it is priced at the shorter tier.
var models = {
  "claude-fable-5-2": { input: 8, cacheWrite5m: 10, cacheWrite1h: 16, cacheRead: 0.20, output: 40 },
  "claude-mythos-5-2": { input: 8, cacheWrite5m: 10, cacheWrite1h: 16, cacheRead: 0.20, output: 40 },
  "claude-fable-5-1": { input: 10, cacheWrite5m: 12.50, cacheWrite1h: 20, cacheRead: 0.25, output: 50 },
  "claude-mythos-5-1": { input: 10, cacheWrite5m: 12.50, cacheWrite1h: 20, cacheRead: 0.25, output: 50 },
  "claude-fable-5": { input: 10, cacheWrite5m: 12.50, cacheWrite1h: 20, cacheRead: 1, output: 50 },
  "claude-mythos-5": { input: 10, cacheWrite5m: 12.50, cacheWrite1h: 20, cacheRead: 1, output: 50 },
  "claude-opus-5-5": { input: 4, cacheWrite5m: 5, cacheWrite1h: 8, cacheRead: 0.20, output: 20 },
  "claude-opus-5": { input: 5, cacheWrite5m: 6.25, cacheWrite1h: 10, cacheRead: 0.50, output: 25 },
  "claude-opus-4-8": { input: 5, cacheWrite5m: 6.25, cacheWrite1h: 10, cacheRead: 0.50, output: 25 },
  "claude-opus-4-7": { input: 5, cacheWrite5m: 6.25, cacheWrite1h: 10, cacheRead: 0.50, output: 25 },
  "claude-opus-4-6": { input: 5, cacheWrite5m: 6.25, cacheWrite1h: 10, cacheRead: 0.50, output: 25 },
  "claude-opus-4-5": { input: 5, cacheWrite5m: 6.25, cacheWrite1h: 10, cacheRead: 0.50, output: 25 },
  "claude-opus-4-1": { input: 15, cacheWrite5m: 18.75, cacheWrite1h: 30, cacheRead: 1.50, output: 75 },
  "claude-opus-4": { input: 15, cacheWrite5m: 18.75, cacheWrite1h: 30, cacheRead: 1.50, output: 75 },
  "claude-sonnet-5-5": { input: 2, cacheWrite5m: 2.50, cacheWrite1h: 4, cacheRead: 0.10, output: 10 },
  "claude-sonnet-5": { input: 2, cacheWrite5m: 2.50, cacheWrite1h: 4, cacheRead: 0.20, output: 10 },
  "claude-sonnet-4-6": { input: 3, cacheWrite5m: 3.75, cacheWrite1h: 6, cacheRead: 0.30, output: 15 },
  "claude-sonnet-4-5": { input: 3, cacheWrite5m: 3.75, cacheWrite1h: 6, cacheRead: 0.30, output: 15 },
  "claude-sonnet-4": { input: 3, cacheWrite5m: 3.75, cacheWrite1h: 6, cacheRead: 0.30, output: 15 },
  "claude-haiku-5-5": { input: 0.10, cacheWrite5m: 0.125, cacheWrite1h: 0.20, cacheRead: 0.01, output: 0.50 },
  "claude-haiku-4-5": { input: 1, cacheWrite5m: 1.25, cacheWrite1h: 2, cacheRead: 0.10, output: 5 }
}
