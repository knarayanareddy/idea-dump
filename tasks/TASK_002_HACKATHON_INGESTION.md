# TASK-002: Hackathon Ideas Ingestion Pipeline (~150 Curated Ideas)

## Objective
Design and execute an automated ingestion script (`ingest_hackathon_ideas.py`) that curates ~150 diverse, high-quality hackathon projects/ideas from popular platforms (Devpost, Builderbase, Devfolio, ETHGlobal, and curated winner archives) and inserts them into the Supabase `ideas` database with `status = 'pending'`.

---

## Architectural Constraints & Requirements

### 1. High-Quality Curation Standards
Each idea must NOT be generic placeholder text. It must include:
- **Title:** Clear, descriptive project name.
- **Raw Content:** Concise problem statement, technical solution architecture, key features, and novelty angle.
- **URLs:** Direct link to the project page, GitHub repository, or showcase URL.
- **Tags:** Domain tags (e.g., `#ai-agents`, `#devtools`, `#computer-vision`, `#fintech`, `#workflow`, `#automation`, `#local-llm`, etc.).
- **Status:** `'pending'` (ready for the daily builder).
- **Priority:** Distributed between `'normal'` and `'high'`.

### 2. Strategy for Sourcing ~150 Ideas
Do NOT attempt to manually scrape single pages one-by-one with slow headless browsers. Instead, implement a high-throughput, deterministic pipeline:
1. Search and aggregate from:
   - Devpost winner feeds / search APIs.
   - Curated GitHub repositories of winning hackathon projects (e.g., `awesome-hackathon-projects`, YC hackathon lists, ETHGlobal finalists, OpenAI/Google AI hackathon showcases).
   - Builderbase / Devfolio public showcase entries.
2. Structure the data into a clean JSON staging file (`scratch/hackathon_ideas_150.json`).
3. Deduplicate against existing ideas in the Supabase database (matching title and URLs).

### 3. Database Ingestion via PostgREST
- Load credentials strictly from `~/.hermes/idea-dump/keys.env`.
- Use Python standard library (`urllib.request`) to perform batch inserts (`POST {SUPABASE_URL}/rest/v1/ideas` with `Prefer: return=minimal` or `Prefer: return=representation`).
- Insert in batches of 25-50 to respect payload limits and network stability.
- Handle rate limits and transient errors gracefully.

### 4. Verification Deliverable
- Execute the ingestion script to populate the Supabase database.
- Verify via PostgREST query that the ideas have been ingested with `status = 'pending'`.
- Check that the total count in the database reflects the new ideas.
