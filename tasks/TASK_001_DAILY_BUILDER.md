# TASK-001: Autonomous Daily Idea Builder (`daily_builder.py`)

## Objective
Build a robust, production-grade autonomous runner script `daily_builder.py` in `/Users/macbookpro/.gemini/antigravity-ide/scratch/idea-dump/` that queries pending ideas from Supabase, orchestrates research/spec generation, validates via JEV gate principles, deterministically creates a GitHub repository, and updates database records.

---

## Architectural Constraints & Requirements

### 1. Security & Configuration
- Load credentials strictly from `~/.hermes/idea-dump/keys.env` (supports `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_ANON_KEY`, etc.).
- Never hardcode API keys or secrets in the script.

### 2. State & Concurrency Guardrails
- **Pause Check:** Check for file `~/.hermes/idea-dump/PAUSED`. If present, log `"Pipeline is PAUSED. Skipping run."` and exit cleanly (code 0).
- **Concurrency Lock:** Use `fcntl.flock` (exclusive, non-blocking) on `~/.hermes/idea-dump/run.lock` to prevent overlapping runs when the machine wakes up or multiple crons trigger.
- **MacBook Sleep Wake Window Check:** Optional check for `--max-stale-minutes` to avoid executing stale queued jobs if the laptop was asleep for days.

### 3. Supabase Integration
- Use Python `urllib.request` (zero extra third-party dependencies required) to interact with Supabase PostgREST API:
  - `GET {SUPABASE_URL}/rest/v1/ideas?status=eq.pending&order=created_at.asc&limit={limit}`
  - `PATCH {SUPABASE_URL}/rest/v1/ideas?id=eq.{id}` to update status (`researching` -> `built` / `archived`).
  - `POST {SUPABASE_URL}/rest/v1/projects` to record newly created repositories.

### 4. Spec Generation & Sub-Agent Coordination
- For candidate ideas:
  - Formulate an OpenSpec-standard change specification (`SPEC.md`), including: Context, Requirements, System Architecture, Phased Implementation Milestones, and Verification Criteria.
  - Formulate `README.md` and `DESCRIPTION.md`.
  - You may call `cline` CLI (with `google/gemini-3.8-flash` or `stealth/space-bunny-alpha`) or Hermes one-shot if synthesis is required, OR synthesize deterministically with clean prompt engineering templates.

### 5. Deterministic GitHub Repository Creation
- Safe slug format: `idea-<slug>` (alphanumeric and dashes only).
- Verify `gh` CLI auth status before creation.
- Check if repo exists: `gh repo view <repo-name>`.
- Create repo: `gh repo create <repo-name> --public --description "..."`.
- Push initial commit containing `README.md`, `SPEC.md`, and any scaffolding.
- Capture repo URL and commit hash.

### 6. CLI Arguments
- `--dry-run`: Query Supabase and generate specs locally in `scratch/output/` without publishing to GitHub or marking database as built.
- `--limit N`: Maximum number of ideas to process per run (default: 1).
- `--idea-id ID`: Target a specific idea ID directly.
- `--check-only`: Test database connection, pause state, and GitHub auth, then exit.

---

## Deliverables
1. `daily_builder.py` fully implemented in `/Users/macbookpro/.gemini/antigravity-ide/scratch/idea-dump/`.
2. Clean error handling, structured logging to stdout and `~/.hermes/idea-dump/log/builder.log`.
3. Validation test: Run `python3 daily_builder.py --check-only` and `python3 daily_builder.py --dry-run` to demonstrate correctness.
