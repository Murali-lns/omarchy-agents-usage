# AI Agents Usage for Omarchy

A native Omarchy bar widget and panel for local usage, limits, pace, recent history, and model statistics from available Claude Code, Codex, Fireworks, Hermes, Grok, and Antigravity records.

## What it does

- Shows available local AI-agent usage records in one bar widget for Claude Code, Codex, Fireworks, Hermes, Grok, and Antigravity.
- Grok and Antigravity provider slots are enabled by default and consume the same standard Omarchy JSON record contract when a compatible collector is installed; the plugin does not scrape or reverse-engineer their private state.
- Displays limits, today’s prompts and sessions, seven-day history, and model token breakdowns.
- Normalizes provider limit records to show only windows actually supplied by upstream providers (session/5-hour, weekly, monthly, reset times, used/limit/remaining, and plan labels).
- Shows a truthful "Limit unavailable / usage-only" status when a provider lacks a safe official quota source, preserving local usage metrics without fabricating quotas.
- Collects Hermes TUI usage from the local `~/.hermes/state.db` database in read-only mode.
- Automatically discovers Hermes provider/subscription routes from recorded models and usage, grouping all models and metrics under safe canonical provider routes (such as OpenAI Codex, OpenCode Go, Grok, Google Gemini, Anthropic, Antigravity, OpenRouter, and Nous).
- Keeps Hermes API transport modes (e.g., chat_completions, codex_responses, anthropic_messages) aggregated under their respective provider routes without splitting subscriptions.
- Never infers subscriptions from model names alone (for example, grok-4.6 routed through OpenCode Go remains OpenCode Go).
- Writes Hermes’ display record to the user’s local `~/.local/state/omarchy/agents/usage/hermes.json`.
- The Hermes collector never reads or sends prompt text, message content, API keys, or credentials, and does not use a network connection.
- Claude Code, Codex, and Fireworks records are collected through Omarchy’s existing provider tools. Grok and Antigravity records are consumed when a compatible Omarchy collector writes them; this plugin does not read their credentials, private databases, or undocumented quota endpoints.
- Optional cross-device aggregation is off by default and is enabled only through the user’s explicit widget settings.

The plugin runs with the user’s normal desktop permissions inside the Omarchy shell. Review the source before enabling it, as with every third-party Omarchy plugin.

## Hermes subscription routes

When your local Hermes installation records usage across backend providers, a nested route option bar appears within the Hermes tab:

- **Automatic discovery & local-only selector**: The option bar discovers routes directly from models and billing providers in local Hermes usage. It allows switching between "All subscriptions" and specific discovered route groups (e.g., OpenAI Codex, OpenCode Go, Grok, Google Gemini, Anthropic, or Unattributed), filtering the Hermes summary, 7-day token chart, activity metrics (API calls, sessions, active days), and model breakdown. This selector and its route data stay local to the machine running Hermes; route/subscription fields are omitted from synced snapshots and are never transmitted or merged across synced devices.
- **Transport mode aggregation**: Transport modes (such as `chat_completions`, `codex_responses`, `anthropic_messages`) are treated as API transport channels rather than subscriptions, and do not divide a single provider subscription into separate routes.
- **Strict attribution boundaries**: Subscriptions are never inferred from model names alone. Missing, unknown, URL-like, or secret-like metadata safely collapses into an explicit Unattributed fallback.
- **Meaning and local accounting**: Route metrics are calculated entirely from the local `~/.hermes/state.db` database (`session_model_usage`). There is no remote quota, plan, balance, or rate-limit lookup against provider APIs. If you configure multiple accounts that share the exact same provider identifier, their metrics are grouped under that provider route.

## Interface example

The panel combines provider tabs, a seven-day token history, and a model-level token breakdown. The values shown below are real local usage statistics from the captured demo machine; every installation displays its own local records.

![AI Agents Usage panel showing provider tabs, token history, and model usage](assets/hermes-usage-panel.png)

## Requirements

- Omarchy Quattro with the Omarchy shell running.
- Python 3 for Hermes collection.
- Omarchy’s built-in agent usage tools for the Claude Code, Codex, and Fireworks records.
- Compatible Grok and Antigravity collectors, if those agents are to appear with local usage; their quotas are shown only when the collector provides them.
- A Hermes installation and local `~/.hermes/state.db` are needed for Hermes statistics; the widget remains usable without Hermes.

## Install

```bash
omarchy plugin add https://github.com/Murali-lns/omarchy-agents-usage.git --enable
```

The standard Omarchy plugin command clones and validates the repository. It runs with ordinary user permissions and this repository has no install hook.

## Remove

```bash
omarchy plugin remove io.github.murali-lns.agents-usage
```

Removing the plugin removes only its installed plugin directory and shell entry. It does not remove Hermes data, agent records, or user configuration.

## Update

```bash
omarchy plugin update io.github.murali-lns.agents-usage
```

## Manual refresh

```bash
omarchy-shell io.github.murali-lns.agents-usage refresh
```

## Privacy and data boundaries

The Hermes collector reads only the active Hermes database path selected by `HERMES_HOME`, or `~/.hermes/state.db` when that variable is unset. It filters to TUI sessions and aggregates counts and token totals by model and discovered provider route. Route/subscription data remains local-only and is deliberately omitted from synced snapshots; synced aggregation contains provider-level count and model totals only. The repository contains no user database, generated usage JSON, shell configuration, cache, credential, API key, prompt, or message content.

## License and attribution

This project is MIT-licensed. Parts of the QML widget are adapted from Omarchy’s Agents shell plugin. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md) for attribution.
