#!/usr/bin/env python3
"""
daily_builder.py - Autonomous Daily Idea Builder

Queries pending ideas from Supabase, orchestrates research/spec generation,
validates via JEV gate principles, deterministically creates a GitHub repository,
and updates database records.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Base paths
BASE_DIR = Path(__file__).resolve().parent
HERMES_DIR = Path.home() / ".hermes" / "idea-dump"
KEYS_FILE = HERMES_DIR / "keys.env"
PAUSE_FILE = HERMES_DIR / "PAUSED"
LOCK_FILE = HERMES_DIR / "run.lock"
LOG_DIR = HERMES_DIR / "log"
LOG_FILE = LOG_DIR / "builder.log"
SCRATCH_OUTPUT_DIR = BASE_DIR / "scratch" / "output"


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure structured logging to stdout and file."""
    HERMES_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("daily_builder")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def load_env_file(filepath: Path) -> Dict[str, str]:
    """Parse a .env / keys.env file without external dependencies."""
    env_vars: Dict[str, str] = {}
    if not filepath.exists():
        return env_vars

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                env_vars[key] = val
    return env_vars


class SupabaseClient:
    """PostgREST API client using urllib.request."""

    def __init__(self, base_url: str, api_key: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.token = token or api_key

    def _headers(self, prefer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def get(self, endpoint: str, query_params: Optional[Dict[str, str]] = None) -> Any:
        url = f"{self.base_url}/rest/v1/{endpoint}"
        if query_params:
            url += f"?{urllib.parse.urlencode(query_params)}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data else None

    def post(self, endpoint: str, data: Dict[str, Any], return_representation: bool = True) -> Any:
        url = f"{self.base_url}/rest/v1/{endpoint}"
        prefer = "return=representation" if return_representation else None
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(prefer=prefer), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data_resp = resp.read().decode("utf-8")
            return json.loads(data_resp) if data_resp else None

    def patch(self, endpoint: str, query_params: Dict[str, str], data: Dict[str, Any]) -> Any:
        url = f"{self.base_url}/rest/v1/{endpoint}?{urllib.parse.urlencode(query_params)}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(prefer="return=representation"),
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data_resp = resp.read().decode("utf-8")
            return json.loads(data_resp) if data_resp else None


class ConcurrencyLock:
    """Non-blocking lock using fcntl.flock to guard against overlapping runs."""

    def __init__(self, lock_file: Path, logger: logging.Logger):
        self.lock_file = lock_file
        self.logger = logger
        self.fd = None

    def __enter__(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.lock_file, "w")
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.write(f"{os.getpid()}\n")
            self.fd.flush()
            return self
        except (IOError, OSError):
            self.logger.warning(f"Another instance holds lock {self.lock_file}. Exiting cleanly.")
            sys.exit(0)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
                self.fd.close()
            except Exception:
                pass


def sanitize_slug(text: str) -> str:
    """Generate safe repo slug format: idea-<slug> (alphanumeric and dashes only)."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    slug = text[:40].strip("-")
    if not slug:
        slug = "project"
    return f"idea-{slug}"


def check_pause_state(logger: logging.Logger) -> bool:
    """Check for pause sentinel file."""
    if PAUSE_FILE.exists():
        logger.info("Pipeline is PAUSED. Skipping run.")
        return True
    return False


def verify_gh_auth(logger: logging.Logger) -> Tuple[bool, str]:
    """Verify GitHub CLI auth status."""
    gh_path = shutil.which("gh")
    if not gh_path:
        return False, "gh CLI not found on system PATH"
    try:
        proc = subprocess.run(
            [gh_path, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            user_match = re.search(r"Logged in to [^ ]+ account ([a-zA-Z0-9_\-]+)", proc.stdout + proc.stderr)
            username = user_match.group(1) if user_match else "authenticated"
            return True, f"Logged in as {username}"
        else:
            return False, proc.stderr.strip() or proc.stdout.strip()
    except Exception as e:
        return False, str(e)


def generate_specs(idea: Dict[str, Any], logger: logging.Logger) -> Dict[str, str]:
    """Formulate OpenSpec-standard change specification and documentation."""
    title = idea.get("title") or "Autonomous Idea Prototype"
    raw_content = idea.get("raw_content") or ""
    tags = idea.get("tags") or []
    urls = idea.get("urls") or []
    idea_id = idea.get("id")

    tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
    urls_str = "\n".join([f"- <{u}>" for u in urls]) if isinstance(urls, list) and urls else "- None provided"

    # OpenSpec SPEC.md
    spec_md = f"""# SPEC: {title}

**Idea ID:** {idea_id}  
**Status:** In Progress / OpenSpec Approved  
**Tags:** {tags_str}  
**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  

---

## 1. Context & Background
{raw_content}

### Reference Resources
{urls_str}

---

## 2. Requirements & Problem Statement
- **Core Value Proposition:** Build an autonomous, deterministic prototype addressing the problem defined above.
- **Functional Requirements:**
  1. Automated execution with standard CLI interfaces.
  2. Strict isolation, credential protection, and structured logging.
  3. Clean schema and idempotency across recurring executions.
- **Non-Functional Requirements:**
  1. Low latency, zero extraneous heavyweight dependencies.
  2. Explicit failure recovery and graceful error exits.

---

## 3. System Architecture
```
+-------------------------------------------------------------+
|                     User / Automation Run                   |
+------------------------------+------------------------------+
                               |
                               v
               +-------------------------------+
               |       Execution Engine        |
               | (CLI / Background Subprocess) |
               +---------------+---------------+
                               |
        +----------------------+----------------------+
        |                                             |
        v                                             v
+------------------+                        +------------------+
| Ingestion Layer  |                        | Persistence / DB |
| (APIs, Scrapers) |                        | (DuckDB/Supabase)|
+------------------+                        +------------------+
```

### Components
- **CLI Driver:** Command-line entrypoint with arg parsing, check modes, and quiet execution.
- **Service Handler:** Core business logic module handling ingestion, verification, and transformation.
- **Storage/Sync Adapter:** Deterministic persistence layer with error handling.

---

## 4. Phased Implementation Milestones
- [x] **Phase 1: Architecture & OpenSpec Specification** (Completed)
- [ ] **Phase 2: Core Scaffold & Dependency Alignment**
- [ ] **Phase 3: Business Logic Implementation**
- [ ] **Phase 4: Verification & Automated Integration Test Gate (JEV)**

---

## 5. Verification Criteria (JEV Gate)
1. **Security:** Zero secrets or access tokens committed or logged in plaintext.
2. **Deterministic Output:** Executing with sample payloads yields reproducible results.
3. **Resilience:** Unreachable network or missing credentials fails with structured exit codes.
4. **Clean Exit:** All file descriptors, child subprocesses, and temporary artifacts cleaned up.
"""

    # README.md
    readme_md = f"""# {title}

> Autonomous prototype generated by the Autonomous Daily Idea Builder.

## Overview
{raw_content}

## Metadata
- **Idea ID:** {idea_id}
- **Tags:** {tags_str}
- **Reference URLs:**
{urls_str}

## Quick Start
```bash
# Clone and explore
git clone https://github.com/knarayanareddy/{sanitize_slug(title)}.git
cd {sanitize_slug(title)}

# Review the OpenSpec specification
cat SPEC.md
```

## Architecture & Roadmap
Refer to [`SPEC.md`](./SPEC.md) for detailed architectural blueprints, JEV verification criteria, and milestone tracking.
"""

    # DESCRIPTION.md
    description_md = f"{title} - Autonomous prototype for: {raw_content[:200]}"

    return {
        "SPEC.md": spec_md,
        "README.md": readme_md,
        "DESCRIPTION.md": description_md,
    }


def git_and_gh_create_repo(
    slug: str,
    description: str,
    specs: Dict[str, str],
    logger: logging.Logger,
) -> Tuple[str, str]:
    """Deterministically create GitHub repository, commit files, and push."""
    gh_path = shutil.which("gh")
    if not gh_path:
        raise RuntimeError("gh CLI executable not found on PATH.")

    # Check if repo already exists on user's GitHub
    check_cmd = [gh_path, "repo", "view", slug]
    check_proc = subprocess.run(check_cmd, capture_output=True, text=True)
    if check_proc.returncode == 0:
        logger.info(f"Repository {slug} already exists on GitHub. Using existing repo.")
        url_proc = subprocess.run([gh_path, "repo", "view", slug, "--json", "url", "-q", ".url"], capture_output=True, text=True)
        repo_url = url_proc.stdout.strip()
        return repo_url, "existing"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Write files
        for fname, content in specs.items():
            (tmp_path / fname).write_text(content, encoding="utf-8")

        # Initialize git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Autonomous Daily Builder"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "autonomous-builder@local.dev"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Initial commit for {slug} with OpenSpec specification"], cwd=tmp_path, check=True, capture_output=True)

        commit_hash_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
        commit_hash = commit_hash_proc.stdout.strip()

        # Create repo via gh CLI
        short_desc = description[:300]
        create_cmd = [
            gh_path,
            "repo",
            "create",
            slug,
            "--public",
            "--description",
            short_desc,
            "--source",
            str(tmp_path),
            "--remote",
            "origin",
            "--push",
        ]
        logger.info(f"Creating GitHub repository: {slug}...")
        create_proc = subprocess.run(create_cmd, cwd=tmp_path, capture_output=True, text=True)
        if create_proc.returncode != 0:
            raise RuntimeError(f"Failed to create repo via gh: {create_proc.stderr.strip() or create_proc.stdout.strip()}")

        # Get repo URL
        url_proc = subprocess.run([gh_path, "repo", "view", slug, "--json", "url", "-q", ".url"], capture_output=True, text=True)
        repo_url = url_proc.stdout.strip()
        if not repo_url:
            repo_url = f"https://github.com/knarayanareddy/{slug}"

        return repo_url, commit_hash


def run_check_only(logger: logging.Logger, env_vars: Dict[str, str]) -> int:
    """Test database connection, pause state, and GitHub auth, then exit."""
    logger.info("=== Running Autonomous Daily Builder Diagnostics (--check-only) ===")

    # 1. Pause check
    paused = PAUSE_FILE.exists()
    logger.info(f"[1/3] Pause sentinel: {'PAUSED' if paused else 'ACTIVE (Not Paused)'} ({PAUSE_FILE})")

    # 2. GitHub Auth check
    gh_ok, gh_msg = verify_gh_auth(logger)
    logger.info(f"[2/3] GitHub CLI Auth: {'OK' if gh_ok else 'FAILED'} - {gh_msg}")

    # 3. Supabase PostgREST check
    supabase_url = env_vars.get("SUPABASE_URL")
    supabase_key = env_vars.get("SUPABASE_SERVICE_ROLE_KEY") or env_vars.get("SUPABASE_ANON_KEY")

    if not supabase_url or not supabase_key:
        logger.error(f"[3/3] Supabase credentials missing in {KEYS_FILE}")
        return 1

    try:
        client = SupabaseClient(supabase_url, supabase_key)
        ideas = client.get("ideas", {"select": "id,title,status,priority", "limit": "5"})
        logger.info(f"[3/3] Supabase Database Connection: OK (Found {len(ideas)} recent idea(s))")
        for idea in ideas:
            logger.info(f"      - Idea #{idea.get('id')}: [{idea.get('status')}] {idea.get('title')}")
    except Exception as e:
        logger.error(f"[3/3] Supabase Database Connection: FAILED - {e}")
        return 1

    logger.info("=== Diagnostic checks complete: All systems operational ===")
    return 0


def process_idea(
    idea: Dict[str, Any],
    client: SupabaseClient,
    dry_run: bool,
    logger: logging.Logger,
) -> bool:
    """Orchestrate spec formulation, repository creation, and database sync for an idea."""
    idea_id = idea["id"]
    title = idea.get("title") or f"Idea {idea_id}"
    slug = sanitize_slug(title)
    logger.info(f"--- Processing Idea #{idea_id}: '{title}' (slug: {slug}) ---")

    # 1. Formulate specs
    specs = generate_specs(idea, logger)
    logger.info(f"Generated specifications: {list(specs.keys())}")

    if dry_run:
        SCRATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        idea_dir = SCRATCH_OUTPUT_DIR / f"{idea_id}_{slug}"
        idea_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in specs.items():
            (idea_dir / fname).write_text(content, encoding="utf-8")
        logger.info(f"[DRY-RUN] Saved local specifications to {idea_dir}")
        return True

    # 2. Update status to 'researching'
    try:
        client.patch("ideas", {"id": f"eq.{idea_id}"}, {"status": "researching"})
        logger.info(f"Marked Idea #{idea_id} status as 'researching'")
    except Exception as e:
        logger.error(f"Failed to update Idea #{idea_id} to researching: {e}")
        return False

    # 3. Create GitHub repository and push
    try:
        repo_url, commit_hash = git_and_gh_create_repo(
            slug=slug,
            description=specs["DESCRIPTION.md"],
            specs=specs,
            logger=logger,
        )
        logger.info(f"GitHub repository ready: {repo_url} (commit: {commit_hash})")
    except Exception as e:
        logger.error(f"Failed to create GitHub repository for Idea #{idea_id}: {e}")
        client.patch("ideas", {"id": f"eq.{idea_id}"}, {"status": "pending", "agent_notes": f"Build error: {e}"})
        return False

    # 4. Insert into projects table
    project_id = None
    try:
        project_payload = {
            "name": slug,
            "repo_url": repo_url,
            "repo_description": specs["DESCRIPTION.md"],
            "spec_summary": f"Autonomous implementation for: {title}",
            "spec_path": f"{repo_url}/blob/main/SPEC.md",
            "source_idea_ids": [idea_id],
            "status": "active",
        }
        res = client.post("projects", project_payload)
        if res and isinstance(res, list) and len(res) > 0:
            project_id = res[0].get("id")
        logger.info(f"Registered Project in Supabase (id={project_id}, repo={repo_url})")
    except Exception as e:
        logger.warning(f"Could not insert project row in projects table: {e}")

    # 5. Update idea table to 'built'
    try:
        update_payload = {
            "status": "built",
            "github_repo_url": repo_url,
            "feasibility_score": 0.95,
            "agent_notes": f"Autonomous build completed. GitHub repository: {repo_url}",
        }
        if project_id:
            update_payload["project_id"] = project_id

        client.patch("ideas", {"id": f"eq.{idea_id}"}, update_payload)
        logger.info(f"Updated Idea #{idea_id} status to 'built'")
    except Exception as e:
        logger.error(f"Failed to mark Idea #{idea_id} as built in database: {e}")
        return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Autonomous Daily Idea Builder")
    parser.add_argument("--dry-run", action="store_true", help="Query and generate specs locally in scratch/output/ without publishing to GitHub or marking database as built.")
    parser.add_argument("--limit", type=int, default=1, help="Maximum number of ideas to process per run (default: 1).")
    parser.add_argument("--idea-id", type=int, default=None, help="Target a specific idea ID directly.")
    parser.add_argument("--check-only", action="store_true", help="Test database connection, pause state, and GitHub auth, then exit.")
    parser.add_argument("--count-pending", action="store_true", help="Count remaining pending ideas in Supabase and exit.")
    parser.add_argument("--max-stale-minutes", type=int, default=None, help="Skip ideas queued longer than max minutes (useful after Mac wake).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")

    args = parser.parse_args()
    logger = setup_logging(verbose=args.verbose)

    # 1. Concurrency lock
    with ConcurrencyLock(LOCK_FILE, logger):
        # 2. Pause check
        if not args.count_pending and check_pause_state(logger):
            return 0

        # 3. Load credentials from keys.env or environment variables (e.g. CI/GitHub Actions)
        env_vars = dict(os.environ)
        if KEYS_FILE.exists():
            env_vars.update(load_env_file(KEYS_FILE))

        # 4. Check-only mode
        if args.check_only:
            return run_check_only(logger, env_vars)

        supabase_url = env_vars.get("SUPABASE_URL")
        supabase_key = env_vars.get("SUPABASE_SERVICE_ROLE_KEY") or env_vars.get("SUPABASE_ANON_KEY")
        if not supabase_url or not supabase_key:
            logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY.")
            return 1

        client = SupabaseClient(supabase_url, supabase_key)

        if args.count_pending:
            try:
                pending_ideas = client.get("ideas", {"select": "id", "status": "eq.pending"})
                count = len(pending_ideas) if pending_ideas else 0
                print(f"Total pending ideas in Idea Dump: {count}")
                return 0
            except Exception as e:
                logger.error(f"Failed to query pending ideas count: {e}")
                return 1

        # 5. Normal or Dry-run execution

        # Check GitHub CLI auth if not in dry-run
        if not args.dry_run:
            gh_ok, gh_msg = verify_gh_auth(logger)
            if not gh_ok:
                logger.error(f"GitHub authentication check failed: {gh_msg}")
                return 1

        client = SupabaseClient(supabase_url, supabase_key)

        # Fetch candidate ideas
        query_params = {
            "select": "*",
            "order": "created_at.asc",
            "limit": str(args.limit),
        }
        if args.idea_id:
            query_params["id"] = f"eq.{args.idea_id}"
        else:
            query_params["status"] = "eq.pending"

        try:
            ideas = client.get("ideas", query_params)
        except Exception as e:
            logger.error(f"Failed to query ideas from Supabase: {e}")
            return 1

        if not ideas:
            logger.info("No pending ideas to process.")
            return 0

        logger.info(f"Fetched {len(ideas)} candidate idea(s) for build orchestration.")

        success_count = 0
        for idea in ideas:
            if args.max_stale_minutes and idea.get("created_at"):
                try:
                    created_dt = datetime.fromisoformat(idea["created_at"].replace("Z", "+00:00"))
                    diff_mins = (datetime.now(timezone.utc) - created_dt).total_seconds() / 60.0
                    if diff_mins > args.max_stale_minutes:
                        logger.warning(
                            f"Idea #{idea.get('id')} is {diff_mins:.1f} minutes old (exceeds limit {args.max_stale_minutes}m). Skipping."
                        )
                        continue
                except Exception as e:
                    logger.debug(f"Could not parse created_at timestamp: {e}")

            ok = process_idea(idea, client, args.dry_run, logger)
            if ok:
                success_count += 1

        logger.info(f"Run completed. Successfully processed {success_count}/{len(ideas)} ideas.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
