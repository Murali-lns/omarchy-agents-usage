# AI Agents Usage for Omarchy

A native Omarchy bar widget and panel for local usage, limits, pace, recent history, and model statistics from Claude Code, Codex, Fireworks, and Hermes.

## What it does

- Shows available local AI-agent usage records in one bar widget.
- Displays limits, today’s prompts and sessions, seven-day history, and model token breakdowns.
- Collects Hermes TUI usage from the local `~/.hermes/state.db` database in read-only mode.
- Writes Hermes’ display record to the user’s local `~/.local/state/omarchy/agents/usage/hermes.json`.
- The Hermes collector never reads or sends prompt text, message content, API keys, or credentials, and does not use a network connection.
- The Claude Code, Codex, and Fireworks records are collected through Omarchy’s existing provider tools and retain their normal local/network and credential behavior.
- Optional cross-device aggregation is off by default and is enabled only through the user’s explicit widget settings.

The plugin runs with the user’s normal desktop permissions inside the Omarchy shell. Review the source before enabling it, as with every third-party Omarchy plugin.

## Interface example

The panel combines provider tabs, a seven-day token history, and a model-level token breakdown. The values shown below are real local usage statistics from the captured demo machine; every installation displays its own local records.

![AI Agents Usage panel showing provider tabs, token history, and model usage](assets/hermes-usage-panel.png)

## Requirements

- Omarchy Quattro with the Omarchy shell running.
- Python 3 for Hermes collection.
- Omarchy’s built-in agent usage tools for the Claude Code, Codex, and Fireworks records.
- A Hermes installation and local `~/.hermes/state.db` are needed for Hermes statistics; the widget remains usable without Hermes.

## Install

```bash
omarchy plugin add https://github.com/Murali-lns/omarchy-agents-usage.git --enable
```

The standard Omarchy plugin command clones and validates the repository. It does not require `sudo` and this repository has no install hook.

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

The Hermes collector reads only the active Hermes database path selected by `HERMES_HOME`, or `~/.hermes/state.db` when that variable is unset. It filters to TUI sessions and aggregates counts and token totals by model. The repository contains no user database, generated usage JSON, shell configuration, cache, credential, API key, prompt, or message content.

## License and attribution

This project is MIT-licensed. Parts of the QML widget are adapted from Omarchy’s Agents shell plugin. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md) for attribution.
