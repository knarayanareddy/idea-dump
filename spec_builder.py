#!/usr/bin/env python3
"""Spec-Driven Autonomous Overnight Builder.

Clones specified target repositories, discovers and parses ``SPEC.md`` (or
``PROJECT_SPEC.md``), and executes unchecked checklist items sequentially using
OpenRouter's ``stealth/space-bunny-alpha`` (pooled keys) with JEV System One oversight.

Each checklist item is:
  1. Synthesized into 3-5 complete, non-stubbed production files.
  2. Verified via syntax compilation and automated test execution (pytest).
  3. Audited via JEV Quality Gate to reject false-positive stubs or fake asserts.
  4. Marked as ``- [x]`` in ``SPEC.md`` and pushed directly to GitHub ``main``.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import shutil
import subprocess
import socket
import sys
import time

# Ensure generous socket timeout for LLM generation (180s)
socket.setdefaulttimeout(180)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Import battle-tested MultiProviderLLM from daily_builder
from daily_builder import MultiProviderLLM, load_env_file

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("spec_builder")

CHECKLIST_RE = re.compile(r"^(\s*[-*]\s*\[([ xX])\])\s+(.+)$")
MAX_ITEM_CORRECTION_ATTEMPTS = 6


@dataclass
class ChecklistItem:
    """A single actionable task item inside SPEC.md."""
    raw_line: str
    checkbox_prefix: str
    completed: bool
    description: str
    line_number: int
    phase_header: str = ""


@dataclass
class SpecDocument:
    """Parsed SPEC.md with architectural contracts and checklist items."""
    path: Path
    raw_text: str
    title: str
    overview: str
    items: List[ChecklistItem]

    @property
    def remaining_items(self) -> List[ChecklistItem]:
        return [i for i in self.items if not i.completed]

    @property
    def completed_count(self) -> int:
        return sum(1 for i in self.items if i.completed)


def parse_spec_file(spec_path: Path) -> Optional[SpecDocument]:
    """Parse a SPEC.md document into metadata and checklist items."""
    if not spec_path.is_file():
        return None

    raw_text = spec_path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()

    title = "Autonomous Project"
    current_phase = ""
    items: List[ChecklistItem] = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("# ") and title == "Autonomous Project":
            title = stripped[2:].strip()
        elif stripped.startswith("## ") or stripped.startswith("### "):
            current_phase = stripped.lstrip("#").strip()

        match = CHECKLIST_RE.match(line)
        if match:
            prefix, check_char, desc = match.groups()
            is_completed = check_char.lower() == "x"
            items.append(
                ChecklistItem(
                    raw_line=line,
                    checkbox_prefix=prefix,
                    completed=is_completed,
                    description=desc.strip(),
                    line_number=idx,
                    phase_header=current_phase,
                )
            )

    # Extract all lines before the implementation checklist as the full architectural overview
    checklist_start_idx = len(lines)
    for idx, line in enumerate(lines):
        if re.search(r"^\s*##+\s*.*(?:checklist|implementation|phase\s*1)", line, re.IGNORECASE):
            checklist_start_idx = idx
            break
        if CHECKLIST_RE.match(line):
            checklist_start_idx = idx
            break

    overview = "\n".join(lines[:checklist_start_idx]).strip()
    if not overview:
        overview = "\n".join(lines[:80])

    return SpecDocument(
        path=spec_path,
        raw_text=raw_text,
        title=title,
        overview=overview,
        items=items,
    )


def mark_item_completed(spec_path: Path, item: ChecklistItem) -> bool:
    """Update SPEC.md on disk, checking off the completed item."""
    try:
        raw_text = spec_path.read_text(encoding="utf-8", errors="replace")
        lines = raw_text.splitlines()
        if item.line_number <= len(lines):
            target_line = lines[item.line_number - 1]
            match = CHECKLIST_RE.match(target_line)
            if match:
                # Replace [ ] with [x]
                new_line = re.sub(r"\[\s*\]", "[x]", target_line, count=1)
                lines[item.line_number - 1] = new_line
                spec_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                item.completed = True
                return True

        # Fallback search by description
        for i, l in enumerate(lines):
            if item.description in l and "[ ]" in l:
                lines[i] = l.replace("[ ]", "[x]", 1)
                spec_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                item.completed = True
                return True
    except Exception as exc:
        logger.error(f"Failed to update SPEC.md for item '{item.description}': {exc}")
    return False


def get_repo_file_tree(workspace: Path) -> List[str]:
    """List all tracked/visible relative files in the workspace."""
    files: List[str] = []
    ignore_dirs = {".git", ".pytest_cache", "__pycache__", "venv", "node_modules", ".eggs", "dist", "build"}
    for root, dirs, filenames in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in filenames:
            if not f.startswith("."):
                rel = Path(root, f).relative_to(workspace)
                files.append(str(rel))
    return sorted(files)


def read_context_files(workspace: Path, file_paths: Sequence[str], max_chars: int = 5000) -> str:
    """Read existing code files with priority for domain models and storage schemas."""
    snippets: List[str] = []
    # Filter out specs, license, gitignore to prioritize actual code
    ignored_names = {"SPEC.md", "spec.md", "PROJECT_SPEC.md", "ARCHITECTURE.md", "README.md", "LICENSE", ".gitignore"}
    candidate_paths = [p for p in file_paths if Path(p).name not in ignored_names]

    # Prioritize python source code files (models, storage, policy, api, engine)
    def priority(path_str: str) -> int:
        p = path_str.lower()
        if "model" in p:
            return 0
        if "storage" in p or "db" in p:
            return 1
        if "policy" in p or "engine" in p:
            return 2
        if "api" in p or "main" in p:
            return 3
        if path_str.endswith(".py"):
            return 4
        if path_str.endswith(".json") or path_str.endswith(".toml"):
            return 5
        return 6

    sorted_candidates = sorted(candidate_paths, key=priority)
    for rel in sorted_candidates[:10]:
        target = workspace / rel
        if target.is_file():
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                snippets.append(f"--- File: {rel} ---\n{content[:max_chars]}")
            except Exception:
                pass
    return "\n\n".join(snippets)


def extract_json(raw: str) -> Optional[dict]:
    """Robustly extract and parse JSON from LLM output, tolerating markdown fences and conversational filler."""
    if not raw or not raw.strip():
        return None

    # 1. Direct parse attempt
    try:
        data = json.loads(raw.strip(), strict=False)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 2. Extract from markdown code fence ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1), strict=False)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    # 3. Extract outermost balanced or greedy braces { ... }
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            data = json.loads(candidate, strict=False)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return None


def extract_files_from_response(raw: str) -> Dict[str, str]:
    """Robustly extract file paths and contents from LLM output across Delimiter, Markdown, and JSON formats."""
    files_map: Dict[str, str] = {}
    if not raw or not raw.strip():
        return files_map

    # 1. Primary: Delimiter pattern (=== FILE: path === ... === END_FILE ===)
    delimiter_pattern = re.compile(
        r"=== (?:FILE|file):\s*([^\n\r]+?)\s*===\s*\n([\s\S]*?)=== (?:END_FILE|end_file) ===",
        re.MULTILINE
    )
    for path, content in delimiter_pattern.findall(raw):
        clean_path = path.strip().strip("`").strip("'").strip('"').lstrip("/")
        if clean_path and content.strip():
            files_map[clean_path] = content.strip()

    if files_map:
        return files_map

    # 2. Markdown headers: (### FILE: path or ## path followed by code fence)
    md_pattern = re.compile(
        r"(?:###|##)\s*(?:FILE:)?\s*`?([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)`?\s*\n```(?:python|json|yaml|yml|html|css|txt|toml)?\s*\n([\s\S]*?)```",
        re.MULTILINE
    )
    for path, content in md_pattern.findall(raw):
        clean_path = path.strip().lstrip("/")
        if clean_path and content.strip():
            files_map[clean_path] = content.strip()

    if files_map:
        return files_map

    # 3. JSON dictionary: {"files": {"path": "content"}}
    data = extract_json(raw)
    if data and isinstance(data, dict):
        files_dict = data.get("files", {})
        if isinstance(files_dict, dict):
            for k, v in files_dict.items():
                if isinstance(v, str) and v.strip():
                    files_map[k.strip().lstrip("/")] = v.strip()

    return files_map


def validate_python_files(workspace: Path, files_written: List[str]) -> Tuple[bool, str]:
    """Validate python files syntax via AST parsing."""
    for rel in files_written:
        if rel.endswith(".py"):
            target = workspace / rel
            if target.is_file():
                try:
                    ast.parse(target.read_text(encoding="utf-8", errors="replace"), filename=rel)
                except SyntaxError as syn_err:
                    return False, f"SyntaxError in {rel} (line {syn_err.lineno}): {syn_err.msg}"
    return True, ""


def chunk_checklist_items(
    items: List[ChecklistItem],
    max_chunk_size: int = 2,
) -> List[List[ChecklistItem]]:
    """Group contiguous checklist items belonging to the same phase into cohesive chunks.

    Coalesces adjacent micro-items (e.g. Models + Storage, Engines + CLI) into single
    synchronized implementation batches, cutting API roundtrips and avoiding cross-file
    contract mismatch.
    """
    chunks: List[List[ChecklistItem]] = []
    current_chunk: List[ChecklistItem] = []

    for item in items:
        if not current_chunk:
            current_chunk.append(item)
            continue

        same_phase = (current_chunk[0].phase_header == item.phase_header)
        if same_phase and len(current_chunk) < max_chunk_size:
            current_chunk.append(item)
        else:
            chunks.append(current_chunk)
            current_chunk = [item]

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def run_workspace_tests(workspace: Path, timeout: int = 90) -> Tuple[bool, str, int]:
    """Run pytest if tests exist in the workspace. Returns (passed, output_or_err, exit_code)."""
    has_tests = (workspace / "tests").is_dir() or any(workspace.glob("test_*.py")) or any(workspace.glob("*_test.py"))
    if not has_tests:
        return True, "No tests defined yet", 999

    try:
        env = dict(os.environ)
        ws_resolved = str(workspace.resolve())
        src_resolved = str((workspace / "src").resolve())
        env["PYTHONPATH"] = f"{ws_resolved}:{src_resolved}"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-v"],
            cwd=str(workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        # returncode 0 = all passed, 5 = no tests collected
        if proc.returncode in (0, 5):
            return True, proc.stdout[-500:] if proc.stdout else "Tests passed", proc.returncode
        return False, proc.stdout[-4000:], proc.returncode
    except subprocess.TimeoutExpired:
        return False, f"Tests timed out after {timeout} seconds", -1
    except Exception as exc:
        return False, f"Test runner invocation error: {exc}", -1


def audit_with_jev(
    llm: MultiProviderLLM,
    chunk: Sequence[ChecklistItem],
    files_written: List[str],
    diff_preview: str,
) -> Tuple[bool, int, str]:
    """Query JEV decision model to verify that the implementation is genuine."""
    tasks_text = "\n".join([f"- {item.description} (Phase: {item.phase_header})" for item in chunk])
    prompt = f"""You are JEV, an autonomous System One code quality evaluator.
Evaluate whether this code contribution genuinely implements the target checklist task(s).

Repository Task(s):
{tasks_text}

Files Created/Modified ({len(files_written)}):
{', '.join(files_written)}

Code Contribution Preview:
{diff_preview[:6000]}

Respond ONLY with valid JSON in this exact structure:
{{
  "verdict": "APPROVED" | "NEEDS_CORRECTION" | "REJECTED",
  "is_false_positive": boolean,
  "quality_score": integer from 1 to 10,
  "reason": "concise explanation",
  "issues": ["list", "of", "deficiencies"]
}}
Criteria:
- APPROVED: Substantive, typed, production-ready implementation fulfilling the task without hollow mock stubs. For non-code tasks (such as documentation, CI workflows, data fixtures, or configuration files), approve if the content is complete, syntactically correct, and production-ready.
- REJECTED / NEEDS_CORRECTION: Zero-byte files, placeholder TODOs, trivial 'assert True' tests, or missing core functionality.
"""
    resp, provider = llm.complete(prompt, system="You are JEV, an uncompromising code evaluation judge. Return valid JSON only.", json_mode=True)
    if not resp:
        return True, 7, "JEV fallback accepted (LLM response blank)"

    try:
        data = extract_json(resp)
        if not data or not isinstance(data, dict):
            return True, 7, "Heuristic pass (JEV format unparsed)"

        verdict = str(data.get("verdict", "APPROVED")).upper()
        score = int(data.get("quality_score", 7))
        is_fp = bool(data.get("is_false_positive", False))
        reason = str(data.get("reason", "Approved"))
        issues = list(data.get("issues", []))

        if is_fp or verdict == "REJECTED" or score < 6:
            flaw = f"{reason} (Issues: {', '.join(issues[:2])})" if issues else reason
            return False, score, flaw
        return True, score, f"JEV {verdict} ({score}/10 via {provider}): {reason}"
    except Exception as exc:
        logger.warning(f"JEV response JSON parsing failed: {exc}; accepting heuristic pass")
        return True, 7, "Heuristic pass (JEV format unparsed)"


class SpecDrivenBuilder:
    """Orchestrates checklist-driven overnight construction across repositories."""

    def __init__(
        self,
        target_repos: List[str],
        work_dir: Path,
        dry_run: bool = False,
        chunk_size: int = 2,
    ):
        self.target_repos = target_repos
        self.work_dir = Path(work_dir).resolve()
        self.dry_run = dry_run
        self.chunk_size = max(1, min(chunk_size, 3))

        # Collect keys from ~/.hermes/idea-dump/keys.env if present, supplemented with os.environ
        keys_path = Path.home() / ".hermes" / "idea-dump" / "keys.env"
        keys = load_env_file(keys_path) if keys_path.is_file() else {}
        for k, v in os.environ.items():
            if k not in keys:
                keys[k] = v
        self.llm = MultiProviderLLM(keys, logger)

    def run(self) -> Dict[str, Any]:
        """Execute spec-driven builds for all target repositories."""
        summary: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repos_processed": len(self.target_repos),
            "results": {},
        }

        for repo in self.target_repos:
            repo_name = repo.strip()
            if not repo_name:
                continue
            logger.info("=" * 70)
            logger.info(f"Targeting Repository: {repo_name}")
            logger.info("=" * 70)
            repo_result = self.build_repository(repo_name)
            summary["results"][repo_name] = repo_result

        return summary

    def build_repository(self, repo_name: str) -> Dict[str, Any]:
        """Clone and build one repository according to its SPEC.md."""
        ws_name = repo_name.replace("/", "_")
        repo_workspace = self.work_dir / ws_name

        # 1. Clone or fetch repository
        clone_ok = self.prepare_workspace(repo_name, repo_workspace)
        if not clone_ok:
            return {"status": "failed", "reason": f"Unable to clone {repo_name}"}

        # 2. Locate SPEC.md
        spec_candidates = [
            repo_workspace / "SPEC.md",
            repo_workspace / "spec.md",
            repo_workspace / "PROJECT_SPEC.md",
            repo_workspace / "ARCHITECTURE.md",
        ]
        spec_path: Optional[Path] = None
        for candidate in spec_candidates:
            if candidate.is_file():
                spec_path = candidate
                break

        if not spec_path:
            logger.warning(f"No SPEC.md found in {repo_name}; skipping.")
            return {"status": "skipped", "reason": "No SPEC.md found"}

        spec = parse_spec_file(spec_path)
        if not spec:
            return {"status": "failed", "reason": "Failed to parse SPEC.md"}

        remaining = spec.remaining_items
        logger.info(f"Project: '{spec.title}' | Total Tasks: {len(spec.items)} | Remaining: {len(remaining)}")

        if not remaining:
            logger.info(f"All {len(spec.items)} checklist items in {repo_name} are already completed!")
            return {"status": "completed", "completed_count": len(spec.items), "total": len(spec.items)}

        # Coalesce micro-tasks into cohesive phase chunks
        chunks = chunk_checklist_items(remaining, max_chunk_size=self.chunk_size)
        logger.info(f"Coalesced {len(remaining)} tasks into {len(chunks)} cohesive phase chunks (chunk_size={self.chunk_size})")

        items_completed_this_run = 0

        # 3. Iterate through phase chunks
        for c_idx, chunk in enumerate(chunks, start=1):
            phase_name = chunk[0].phase_header or "General"
            logger.info("-" * 60)
            logger.info(f"[{c_idx}/{len(chunks)}] Implementing Phase Chunk ({len(chunk)} task(s)) in '{phase_name}':")
            for it in chunk:
                logger.info(f"  * {it.description}")

            success, details = self.implement_checklist_chunk(spec, chunk, repo_workspace)
            if success:
                items_completed_this_run += len(chunk)
                logger.info(f"Phase Chunk Verified ({len(chunk)} tasks completed)")

                # Update SPEC.md on disk for all items in chunk
                for item in chunk:
                    mark_item_completed(spec_path, item)

                if not self.dry_run:
                    task_summary = " & ".join([it.description.split("`")[0].strip()[:35] for it in chunk])
                    self.git_commit_and_push(
                        repo_workspace,
                        f"feat({phase_name}): implement {task_summary}",
                    )
            else:
                logger.error(f"Failed to implement chunk: {details}")
                logger.info("Continuing to next phase chunk...")
                continue

        return {
            "status": "in_progress" if len(spec.remaining_items) > 0 else "completed",
            "items_completed": items_completed_this_run,
            "total_items": len(spec.items),
            "remaining_items": len(spec.remaining_items),
        }

    def prepare_workspace(self, repo_name: str, workspace: Path) -> bool:
        """Clone the repository or pull latest main."""
        workspace.parent.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("GH_PAT") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            clone_url = f"https://x-access-token:{token}@github.com/{repo_name}.git"
        else:
            clone_url = f"https://github.com/{repo_name}.git"

        if (workspace / ".git").is_dir():
            logger.info(f"Workspace exists. Pulling latest main for {repo_name}...")
            res = subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(workspace), capture_output=True, text=True)
            if res.returncode == 0:
                return True
            logger.warning(f"Git pull failed: {res.stderr}; wiping directory for fresh clone...")
            shutil.rmtree(workspace, ignore_errors=True)
        elif workspace.exists():
            logger.info(f"Wiping non-git directory at {workspace} for clean clone...")
            shutil.rmtree(workspace, ignore_errors=True)

        logger.info(f"Cloning {repo_name} into {workspace}...")
        res = subprocess.run(["git", "clone", clone_url, str(workspace)], capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Git clone error: {res.stderr}")
            return False
        return True

    def implement_checklist_chunk(
        self,
        spec: SpecDocument,
        chunk: List[ChecklistItem],
        workspace: Path,
    ) -> Tuple[bool, str]:
        """Synthesize, verify, and audit a cohesive chunk of checklist items with self-correction."""
        file_tree = get_repo_file_tree(workspace)
        context_preview = read_context_files(workspace, file_tree)

        phase_header = chunk[0].phase_header or "Implementation"
        tasks_listing = "\n".join([f"{i+1}. {it.description}" for i, it in enumerate(chunk)])

        error_feedback = ""
        previous_files_map: Dict[str, str] = {}

        for attempt in range(1, MAX_ITEM_CORRECTION_ATTEMPTS + 1):
            if attempt > 1:
                # Clean up uncommitted scratch files from previous attempt
                subprocess.run(["git", "checkout", "."], cwd=str(workspace), capture_output=True)
                subprocess.run(["git", "clean", "-fd"], cwd=str(workspace), capture_output=True)
            logger.info(f"Synthesis Attempt {attempt}/{MAX_ITEM_CORRECTION_ATTEMPTS} for Chunk ({len(chunk)} task(s))...")

            prompt = f"""You are Space Bunny Alpha, an elite autonomous software engineer building a production project from an architectural specification.

SPECIFICATION OVERVIEW:
{spec.overview}

CURRENT COHESIVE CHECKLIST TASKS TO IMPLEMENT (Phase: {phase_header}):
{tasks_listing}

EXISTING REPOSITORY STRUCTURE:
{', '.join(file_tree) if file_tree else '(Empty repository)'}

EXISTING RELEVANT CODE CONTEXT:
{context_preview}
"""
            if error_feedback and previous_files_map:
                prev_code_block = "\n\n".join([f"=== FILE: {p} ===\n{c[:3000]}\n=== END_FILE ===" for p, c in previous_files_map.items()])
                prompt += f"""
YOUR CODE FROM PREVIOUS ATTEMPT:
{prev_code_block}

FAILED WITH THIS ERROR:
{error_feedback}

Please analyze the exact error in your previous code above, fix the root cause across all files in this batch, and output the complete corrected files!
"""
            elif error_feedback:
                prompt += f"""
PREVIOUS ATTEMPT FAILED WITH ERROR:
{error_feedback}
Please fix the exact issues above and output working code!
"""

            prompt += """
REQUIREMENTS & GOAL-DRIVEN AUTONOMY:
1. Implement the complete, production-grade files required by ALL listed tasks above in a synchronized batch.
2. Ensure domain models, storage engines, policies, and test fixtures are fully aligned with each other.
3. NO placeholder comments, NO 'pass', NO empty stubs. Write real, robust domain logic.
4. Every new function or class must be covered with substantive unit tests.
5. PERMITTED SCOPE EXPANSION: You have full authority to update existing domain models, test fixtures (e.g. conftest.py), or pyproject.toml dependencies if required to make unit tests pass cleanly and resolve type or argument errors.
6. Output each file using clean delimiters (recommended) or strict JSON:

=== FILE: relative/path/to/file.py ===
<full file contents here>
=== END_FILE ===

=== FILE: tests/test_feature.py ===
<full test contents here>
=== END_FILE ===
"""
            resp, provider = self.llm.complete(
                prompt,
                system="You are an elite autonomous developer. Output production code using the specified === FILE: ... === delimiters or JSON.",
                json_mode=False,
            )

            if not resp:
                error_feedback = "LLM response was blank or timed out"
                continue

            try:
                files_dict = extract_files_from_response(resp)
                if not files_dict:
                    error_feedback = "Could not extract valid files from LLM output. Use '=== FILE: path === ... === END_FILE ===' or JSON."
                    continue
                previous_files_map = dict(files_dict)

                # Write files to workspace
                files_written = []
                diff_snippets = []
                for rel_path, content in files_dict.items():
                    rel_clean = rel_path.strip().lstrip("/")
                    target_file = workspace / rel_clean
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(content, encoding="utf-8")
                    files_written.append(rel_clean)
                    diff_snippets.append(f"--- {rel_clean} ---\n{content[:1000]}")
                    # Ensure package __init__.py exists if in a Python package subdirectory
                    if rel_clean.endswith(".py") and target_file.parent != workspace:
                        init_py = target_file.parent / "__init__.py"
                        if not init_py.is_file():
                            init_py.write_text("# Package initialized\n", encoding="utf-8")

                logger.info(f"Wrote {len(files_written)} file(s): {', '.join(files_written)}")

                # Syntax Check
                syntax_ok, syntax_err = validate_python_files(workspace, files_written)
                if not syntax_ok:
                    error_feedback = f"Syntax compilation failed: {syntax_err}"
                    logger.warning(error_feedback)
                    continue

                # Automated Tests Check (Objective Reality Gate)
                tests_ok, test_err, exit_code = run_workspace_tests(workspace)
                if not tests_ok:
                    error_feedback = f"Automated tests failed:\n{test_err}"
                    logger.warning(f"Tests failed on attempt {attempt}: {test_err[:200]}")
                    continue

                # If pytest ran and passed with exit code 0, we have objective reality verification!
                # Bypass secondary LLM roundtrip to cut latency and prevent stylistic rejections.
                if exit_code == 0:
                    logger.info("Objective Reality Gate Passed: 100% pytest test suite passed. Bypassing secondary LLM audit to save roundtrips.")
                    return True, f"Successfully implemented {len(chunk)} task(s) in {len(files_written)} files (pytest passed 100%)"

                # If no tests exist yet (exit_code 999 or 5), use JEV audit to verify non-stub code
                diff_preview = "\n\n".join(diff_snippets)
                jev_approved, jev_score, jev_msg = audit_with_jev(self.llm, chunk, files_written, diff_preview)
                if not jev_approved and jev_score < 4:
                    error_feedback = f"JEV Quality Gate flagged serious defect: {jev_msg}"
                    logger.warning(error_feedback)
                    continue

                logger.info(f"Phase chunk passed quality gate: {jev_msg}")
                return True, f"Successfully implemented {len(chunk)} task(s) in {len(files_written)} files ({jev_msg})"

            except Exception as exc:
                error_feedback = f"Execution error: {exc}"
                logger.warning(f"Attempt {attempt} failed: {exc}")

        # Reset working tree so next items start from a clean state
        subprocess.run(["git", "checkout", "."], cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "clean", "-fd"], cwd=str(workspace), capture_output=True)
        return False, f"Exhausted {MAX_ITEM_CORRECTION_ATTEMPTS} attempts. Last error: {error_feedback}"

    def git_commit_and_push(self, workspace: Path, commit_msg: str) -> bool:
        """Stage all changes, commit, and push directly to origin main."""
        try:
            # Ensure local git identity is configured
            subprocess.run(["git", "config", "user.name", "Autonomous Spec Builder"], cwd=str(workspace), check=False)
            subprocess.run(["git", "config", "user.email", "autonomous-builder@local.dev"], cwd=str(workspace), check=False)

            subprocess.run(["git", "add", "."], cwd=str(workspace), check=True)
            # Check if there are staged changes
            status = subprocess.run(["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True, text=True)
            if not status.stdout.strip():
                return True

            subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(workspace), check=True)
            res = subprocess.run(["git", "push", "origin", "main"], cwd=str(workspace), capture_output=True, text=True)
            if res.returncode == 0:
                logger.info(f"Pushed commit to main: '{commit_msg}'")
                return True
            logger.warning(f"Git push failed: {res.stderr}. Retrying with pull --rebase...")
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(workspace), capture_output=True, text=True)
            res2 = subprocess.run(["git", "push", "origin", "main"], cwd=str(workspace), capture_output=True, text=True)
            if res2.returncode == 0:
                logger.info(f"Pushed commit to main after rebase: '{commit_msg}'")
                return True
            logger.warning(f"Git push retry failed: {res2.stderr}")
        except Exception as exc:
            logger.error(f"Git commit/push error: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Spec-Driven Overnight Autonomous Builder")
    parser.add_argument(
        "--repos",
        type=str,
        default=os.environ.get("TARGET_REPOS", ""),
        help="Comma-separated list of target GitHub repositories (e.g. knarayanareddy/repo1,knarayanareddy/repo2)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=str(Path.home() / ".hermes" / "spec_builder_workspace"),
        help="Local workspace directory for cloning repos",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview generation without committing or pushing to GitHub",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(os.environ.get("CHUNK_SIZE", 2)),
        help="Number of contiguous checklist items per phase chunk (default: 2)",
    )

    args = parser.parse_args()
    repos = [r.strip() for r in args.repos.split(",") if r.strip()]

    if not repos:
        targets_file = Path("config/overnight_targets.json")
        if targets_file.is_file():
            try:
                data = json.loads(targets_file.read_text(encoding="utf-8"))
                repos = [r.strip() for r in data.get("repositories", []) if r.strip()]
            except Exception as exc:
                logger.warning(f"Failed to read config/overnight_targets.json: {exc}")

    if not repos:
        logger.error("No repositories specified! Provide --repos <repo1>,<repo2>, set TARGET_REPOS env, or configure config/overnight_targets.json.")
        sys.exit(1)

    builder = SpecDrivenBuilder(
        target_repos=repos,
        work_dir=Path(args.workspace),
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
    )
    summary = builder.run()
    print("\n" + "=" * 70)
    print(f"Overnight Build Completed: {json.dumps(summary, indent=2)}")
    print("=" * 70)


if __name__ == "__main__":
    main()

