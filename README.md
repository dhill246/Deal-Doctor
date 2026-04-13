# Deal Doctor

A pipeline diagnosis tool that connects to your HubSpot portal, pulls your deal data, and tells you what's healthy and what needs attention. Runs inside [Claude Code](https://claude.ai/code) — no coding required.

---

## Getting Started

### Step 1: Install Claude Code

If you don't already have it, grab [Claude Code](https://claude.ai/code) — it's the AI assistant that drives this whole analysis. Available as a CLI, desktop app (Mac/Windows), or web app.

### Step 2: Install Python

The analysis scripts need Python to run. If you're not sure whether you have it:

**Windows:**
1. Open your Start menu and type **"python"** — if Python shows up as an installed app, you're good
2. If not, go to [python.org/downloads](https://python.org/downloads) and click the big **"Download Python"** button
3. Run the installer — **check the box that says "Add Python to PATH"** at the bottom of the first screen
4. Click "Install Now"

**Mac:**
1. Open Terminal and type `python3 --version` — if you see a version number (3.10 or higher), you're good
2. If not, go to [python.org/downloads](https://python.org/downloads) and download the Mac installer

You need Python 3.10 or higher. If you already have Python installed, you're all set.

### Step 3: Create a HubSpot API Token

You need a token so the tool can read your deals. It takes about 2 minutes and only grants **read** access — it cannot modify anything in your portal.

1. Log into HubSpot and click the **gear icon** (Settings) in the top navigation bar
2. In the left sidebar, go to **Integrations > Private Apps**
3. Click **"Create a private app"**
4. Give it a name (e.g., "Deal Doctor")
5. Click the **Scopes** tab
6. Search for **crm.objects.deals.read** and check the box
7. Click **"Create app"** in the top right, then **"Continue creating"** on the confirmation dialog
8. Copy your access token — it starts with `pat-`

Keep this token handy for the next step.

### Step 4: Clone the Repo and Add Your Token

```bash
git clone https://github.com/MorphDataStrategies/deal-doctor.git
cd deal-doctor
```

Copy the example environment file to create your own:

```bash
cp .env.example .env
```

Or just rename `.env.example` to `.env`. Then open it in any text editor (Notepad, VS Code, TextEdit — whatever you have). You'll see a placeholder line. Replace it with your token so the file looks like this:

```
HUBSPOT_ACCESS_TOKEN=pat-na1-your-actual-token-here
```

Save the file.

> **What's a `.env` file?** It's just a plain text file that stores your API token locally on your computer. It's listed in `.gitignore`, which means it never gets uploaded or shared — your token stays private.

### Step 5: Run the Analysis

Open Claude Code in this project folder, then type:

```
/deals
```

That's it. Claude will verify your setup, connect to your HubSpot, and walk you through the analysis conversationally. If anything isn't configured right, it'll help you fix it before proceeding.

---

## What You Get

**An interactive HTML report** you can open in any browser:

- Interactive Plotly charts — hover for details, zoom, filter
- Click any table row to drill down into the actual deals behind the numbers
- Dark/light mode toggle
- What-if calculator — model the revenue impact of improving win rates
- "Dig Deeper" section with follow-up questions to bring back to the conversation

**Sections include** (only the ones relevant to your data):

- Executive Summary, Win/Loss Breakdown, Deal Size Distribution
- Win Rate by Deal Size Tier, Sales Cycle Analysis, Deal Velocity
- Revenue Concentration (Lorenz curve), Monthly/Quarterly Trends
- Rep Performance, Lead Source Analysis, Loss Reasons, Stage Drop-off

---

## Requirements

- [Claude Code](https://claude.ai/code) (free or Pro)
- Python 3.10+
- A HubSpot account with closed deals (works with any HubSpot tier — Free, Starter, Pro, or Enterprise)

---

## FAQ

**Will this modify anything in my HubSpot?**
No. The token only has read access to deals. Nothing is created, updated, or deleted.

**How many deals do I need?**
The tool works with as few as 10 closed deals, but you'll get the most insight with 50+. It automatically skips sections that don't have enough data to be meaningful.

**What if I have multiple pipelines?**
The interactive flow will show you all your pipelines and let you pick which one to analyze, or analyze all of them together.

**Can I run it again with different parameters?**
Absolutely. Just type `/deals` again and choose different options.

**Do I need to know Python?**
No. Claude Code handles everything — installing packages, running the script, interpreting results. You just answer questions about what you want to see.

**I'm getting an error about my token.**
Open the `.env` file and make sure it looks exactly like `HUBSPOT_ACCESS_TOKEN=pat-na1-...` with no extra spaces, quotes, or line breaks. The token should start with `pat-`.

**What if Claude Code says Python isn't installed?**
Close and reopen Claude Code after installing Python — it needs to pick up the new PATH. If you just installed Python on Windows, make sure you checked "Add Python to PATH" during installation.

---

## About

Built by [Morph Data Strategies](https://morphdatastrategies.com) — we help revenue teams fix their HubSpot and build systems that actually drive pipeline.

If this report surfaces issues you want help solving, [let's talk](https://morphdatastrategies.com).
