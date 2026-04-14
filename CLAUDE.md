# Deal Doctor

A pipeline diagnosis tool that connects to a user's HubSpot portal, pulls their deal data, analyzes it, and generates an interactive report.

## How This Works

- `deal_analyzer.py` is the toolkit — discrete commands for fetching, computing, and reporting
- `html_report.py` + `report_template.html` generate the interactive HTML report
- `.env` holds the user's `HUBSPOT_ACCESS_TOKEN`
- `analysis_config.json` holds the analysis parameters (generated interactively)

## End-to-End Flow

```
check → init → fetch → metrics → report
```

1. `py deal_analyzer.py check` — Validates .env, token, and HubSpot scopes. Run this first.
2. `py deal_analyzer.py init` — Fetches pipelines, auto-detects won/lost stages, writes `analysis_config.json`. Use `--pipeline <id>` if multiple pipelines exist.
3. `py deal_analyzer.py fetch --config analysis_config.json` — Pulls deals from HubSpot, saves to `deals.json`.
4. `py deal_analyzer.py metrics --data deals.json` — Computes all metrics, saves to `analysis.json`.
5. `py deal_analyzer.py report --data analysis.json --sections "sec1,sec2" --findings 'JSON'` — Generates the interactive HTML report.

## Commands

- `py deal_analyzer.py check` — Validates .env exists, token is set, and each HubSpot scope works. Returns JSON with pass/fail per scope and tells you exactly what to fix.
- `py deal_analyzer.py init` — Auto-detects pipelines and closed won/lost stages (probability 1.0/0.0), writes `analysis_config.json`. If multiple pipelines, returns the list so you can pick one with `--pipeline <id>`.
- `py deal_analyzer.py schema` — Fetches pipelines, stages, and owners. Returns JSON.
- `py deal_analyzer.py fetch --config analysis_config.json` — Fetches deals, saves to `deals.json`, returns summary stats.
- `py deal_analyzer.py metrics --data deals.json` — Computes all metrics, saves to `analysis.json`, returns full JSON.
- `py deal_analyzer.py report --data analysis.json --sections "sec1,sec2" --findings 'JSON'` — Generates HTML report with selected sections and custom findings.
- `py deal_analyzer.py analyze --config analysis_config.json` — Legacy: runs full pipeline and generates PDF.

### Report Sections

Available section IDs for `--sections`: `executive_summary`, `win_loss`, `deal_size`, `tier_analysis`, `sales_cycle`, `cycle_vs_size`, `deal_velocity`, `revenue_concentration`, `time_trends`, `quarterly_trends`, `rep_performance`, `source_analysis`, `loss_reasons`, `stage_dropoff`

### Findings Format

The `--findings` parameter accepts a JSON array: `[{"text": "Finding 1"}, {"text": "Finding 2"}]`

## Important

- Always use `py` to run Python (not `python` or `python3`); fall back to `python3` on Mac/Linux
- The `/deals` skill handles all setup automatically — it checks for Python, installs dependencies, validates the HubSpot token, and walks users through anything that's missing
- The `.env` file must contain `HUBSPOT_ACCESS_TOKEN=pat-...` — NEVER ask users to paste tokens into the terminal. Always direct them to open `.env` in a text editor.
- Stage IDs are numeric strings (e.g., "962926111") — always fetch schema to discover them; never guess
- The tool auto-detects closed won stages (probability 1.0) and closed lost stages (probability 0.0) from pipeline metadata

## User Context

The target user is a someone who is technical enough to use Claude Code but not a developer. They may not have Python installed or know what a `.env` file is. The `/deals` skill handles onboarding — it checks prerequisites step by step and guides users through anything that's not ready. Be conversational and brief. Don't dump raw output — interpret the results.
