# Idea Dump • Autonomous Engineering Pipeline

A high-volume idea ingestion stream and daily autonomous project factory. Dump raw notes, URLs, and feature combinations on your laptop or phone—and let your MacBook's **Hermes (GLM-5.3)** & **Cline (Gemini 3.8 Flash)** agents research, generate OpenSpec architecture specifications, and ship a new GitHub repository every day.

---

## Architecture Overview

```
                      [ GitHub.io / Mobile Browser / Laptop ]
                                        │
                                        ▼ (Instant HTTPS Insert)
                      [ Supabase PostgreSQL Free Tier ]
                                 (ideas table)
                                        ▲
                                        │ (Daily 07:00 Cron Query: WHERE status = 'pending')
                      [ MacBook Autonomous Agent Engine ]
                                        │
            ┌───────────────────────────┴───────────────────────────┐
            ▼                                                       ▼
  [ Hermes Agent (Tinker GLM-5.3) ]                     [ Cline CLI (Gemini 3.8 Flash) ]
  • Inbox triage & category match                      • 1M-token context deep research
  • Cross-idea synergy planning                         • Full OpenSpec & code synthesis
            │                                                       │
            └───────────────────────────┬───────────────────────────┘
                                        ▼
                            [ JEV Office Gate ]
                            • Evidence & claim check
                                        │
                                        ▼
                      [ Deterministic GitHub Publisher ]
                      • gh repo create idea-<slug>
                      • UPDATE ideas SET status = 'built', github_repo_url = ...
```

---

## 1. Quick Start: Local Testing

Open `index.html` in your browser:
```bash
open /Users/macbookpro/.gemini/antigravity-ide/scratch/idea-dump/index.html
```
The app runs immediately in **Demo Mode (Local Storage)**. You can dump ideas, attach links, add tags, and test the filters with zero setup.

---

## 2. Setting Up Free Supabase (PostgreSQL)

1. Create a free account at [supabase.com](https://supabase.com).
2. Create a new project (e.g. `idea-dump`).
3. Open the **SQL Editor** in the Supabase dashboard and run the entire [`schema.sql`](schema.sql) file.
4. Go to **Project Settings > API**:
   * Copy the **Project URL** (e.g. `https://xyz.supabase.co`).
   * Copy the **anon public** API key.
5. In your `index.html` page, click **Connect DB** in the top right, paste both values, and click **Save & Connect**.
6. The status badge will turn **🟢 Connected (Supabase)**. All your dumped ideas will now persist directly into your cloud PostgreSQL database!

---

## 3. Deploying to GitHub Pages (`github.io`)

1. Create a new GitHub repository (e.g. `idea-dump` or your personal user page `username.github.io`).
2. Push this folder:
   ```bash
   cd /Users/macbookpro/.gemini/antigravity-ide/scratch/idea-dump
   git init
   git add .
   git commit -m "feat: initial idea-dump dashboard and supabase schema"
   gh repo create idea-dump --public --source=. --push
   ```
3. In your repo settings on GitHub, navigate to **Pages**, select **Deploy from a branch** (`main` / root), and click **Save**.
4. Your dashboard is now live on the internet at `https://<your-username>.github.io/idea-dump`! Bookmark it on your phone home screen for instant 2-second idea dumping.

---

## 4. Supabase Schema Reference

* **`ideas`**: Stores every raw entry with auto-computed `day_of_week`, extracted `urls`, `tags`, and pipeline status (`pending`, `researching`, `spec_ready`, `built`, `archived`).
* **`projects`**: Tracks the GitHub repositories created daily by Hermes & Cline, linking back to `source_idea_ids`.
* **RLS Policies**: Fully configured so your static `github.io` page can read and insert safely using the public anon key without backend servers.

---

## 5. Hackathon Idea Ingestion (`ingest_hackathon_ideas.py`)

Seeds the `ideas` table with real, award-winning hackathon projects so the daily
builder always has a deep backlog of `status = 'pending'` work.

```bash
python3 ingest_hackathon_ideas.py --check-only     # credentials + connectivity
python3 ingest_hackathon_ideas.py --dry-run        # harvest + dedupe, no writes
python3 ingest_hackathon_ideas.py --harvest-only   # build staging JSON only
python3 ingest_hackathon_ideas.py --staging-only   # insert from staging JSON
python3 ingest_hackathon_ideas.py --repair-titles  # upgrade weak titles in place
python3 ingest_hackathon_ideas.py --verify-only    # print table counts
```

**Sources.** `devpost.com` is behind bot protection (HTTP 403 / DataDome) and
`ethglobal.com` is a client-rendered SPA, so neither is scraped directly.
Instead the pipeline harvests the public source repositories those projects
publish, using the `ethglobal` and `devpost` topics plus a curated winner
archive (`unicorn-mafia/awesome-hackathon-winners`). 1,277 candidate
repositories are discovered; the top 150 by signal are enriched and inserted.

**No invented data.** Every field is derived from real harvested GitHub
metadata and the projects' own READMEs — names, URLs, descriptions, feature
bullets, detected stack and verbatim award mentions. `raw_content` is a
deterministically composed briefing (problem, architecture, features, novelty,
validation) built from those real signals.

**Controls.** Credentials are read strictly from `~/.hermes/idea-dump/keys.env`.
Rows are deduplicated against both the staging set and the live table on
normalized title *and* URL overlap, then inserted in batches of 25 via
`POST /rest/v1/ideas` with `Prefer: return=minimal,count=exact`, with
exponential backoff on 429/5xx and network errors. The staging artifact is
written to `scratch/hackathon_ideas_150.json`; logs go to
`~/.hermes/idea-dump/log/ingest.log`.
