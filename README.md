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
