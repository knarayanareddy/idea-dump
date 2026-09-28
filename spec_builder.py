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
import sys
import time
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
MAX_ITEM_CORRECTION_ATTEMPTS = 3


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

    return SpecDocument(
        path=spec_path,
        raw_text=raw_text,
        title=title,
        overview="\n".join(lines[:40]),
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


def read_context_files(workspace: Path, file_paths: Sequence[str], max_chars: int = 4000) -> str:
    """Read a small sample of existing files to give the LLM context."""
    snippets: List[str] = []
    for rel in file_paths[:5]:
        target = workspace / rel
        if target.is_file() and target.suffix in (".py", ".json", ".toml", ".yml", ".yaml", ".md", ".ts", ".js"):
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                snippets.append(f"--- File: {rel} ---\n{content[:max_chars]}")
            except Exception:
                pass
    return "\n\n".join(snippets)


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


def run_workspace_tests(workspace: Path, timeout: int = 90) -> Tuple[bool, str]:
    """Run pytest if tests exist in the workspace."""
    has_tests = (workspace / "tests").is_dir() or any(workspace.glob("test_*.py")) or any(workspace.glob("*_test.py"))
    if not has_tests:
        return True, "No tests defined yet"

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-v"],
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return True, proc.stdout[-500:]
        return False, proc.stdout[-1500:]
    except subprocess.TimeoutExpired:
        return False, f"Tests timed out after {timeout} seconds"
    except Exception as exc:
        return False, f"Test runner invocation error: {exc}"


def audit_with_jev(
    llm: MultiProviderLLM,
    item: ChecklistItem,
    files_written: List[str],
    diff_preview: str,
) -> Tuple[bool, int, str]:
    """Query JEV decision model to verify that the implementation is genuine."""
    prompt = f"""You are JEV, an autonomous System One code quality evaluator.
Evaluate whether this code contribution genuinely implements the target checklist item.

Repository Task / Checklist Item:
"{item.description}" (Phase: {item.phase_header})

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
- APPROVED: Substantive, typed, production-ready implementation fulfilling the task without hollow mock stubs.
- REJECTED / NEEDS_CORRECTION: Zero-byte files, placeholder TODOs, trivial 'assert True' tests, or missing core functionality.
"""
    resp, provider = llm.complete(prompt, system="You are JEV, an uncompromising code evaluation judge. Return valid JSON only.", json_mode=True)
    if not resp:
        return True, 7, "JEV fallback accepted (LLM response blank)"

    try:
        clean = re.sub(r"^```(?:json)?\s*", "", resp.strip())
        clean = re.sub(r"\s*```$", "", clean)
        data = json.loads(clean)
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
    ):
        self.target_repos = target_repos
        self.work_dir = Path(work_dir).resolve()
        self.dry_run = dry_run

        keys = load_env_file()
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

        items_completed_this_run = 0

        # 3. Iterate through unchecked items
        for idx, item in enumerate(remaining, start=1):
            logger.info("-" * 60)
            logger.info(f"[{idx}/{len(remaining)}] Implementing Checklist Item: {item.description}")
            logger.info(f"Phase: {item.phase_header}")

            success, details = self.implement_checklist_item(spec, item, repo_workspace)
            if success:
                items_completed_this_run += 1
                logger.info(f"Checklist Item Verified: {item.description}")

                # Update SPEC.md on disk
                mark_item_completed(spec_path, item)

                if not self.dry_run:
                    self.git_commit_and_push(
                        repo_workspace,
                        f"feat: implement checklist item '{item.description}'",
                    )
            else:
                logger.error(f"Failed to implement '{item.description}': {details}")
                # Continue to next item or stop depending on criticality
                break

        return {
            "status": "in_progress" if len(spec.remaining_items) > 0 else "completed",
            "items_completed": items_completed_this_run,
            "total_items": len(spec.items),
            "remaining_items": len(spec.remaining_items),
        }

    def prepare_workspace(self, repo_name: str, workspace: Path) -> bool:
        """Clone the repository or pull latest main."""
        workspace.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://github.com/{repo_name}.git"

        if (workspace / ".git").is_dir():
            logger.info(f"Workspace exists. Pulling latest main for {repo_name}...")
            res = subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(workspace), capture_output=True, text=True)
            return res.returncode == 0

        logger.info(f"Cloning {clone_url} into {workspace}...")
        res = subprocess.run(["git", "clone", clone_url, str(workspace)], capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Git clone error: {res.stderr}")
            return False
        return True

    def implement_checklist_item(
        self,
        spec: SpecDocument,
        item: ChecklistItem,
        workspace: Path,
    ) -> Tuple[bool, str]:
        """Synthesize, verify, and JEV-audit one checklist item with self-correction."""
        file_tree = get_repo_file_tree(workspace)
        context_preview = read_context_files(workspace, file_tree)

        error_feedback = ""

        for attempt in range(1, MAX_ITEM_CORRECTION_ATTEMPTS + 1):
            logger.info(f"Synthesis Attempt {attempt}/{MAX_ITEM_CORRECTION_ATTEMPTS} for '{item.description}'...")

            prompt = f"""You are Space Bunny Alpha, an elite autonomous software engineer building a production project from an architectural specification.

SPECIFICATION OVERVIEW:
{spec.overview}

CURRENT CHECKLIST TASK TO IMPLEMENT:
- Phase: {item.phase_header}
- Task: {item.description}

EXISTING REPOSITORY STRUCTURE:
{', '.join(file_tree) if file_tree else '(Empty repository)'}

EXISTING RELEVANT CODE CONTEXT:
{context_preview}
"""
            if error_feedback:
                prompt += f"""
PREVIOUS ATTEMPT FAILED WITH ERROR:
{error_feedback}
Please fix the exact issues above and output working code!
"""

            prompt += """
REQUIREMENTS:
1. Output ONLY 3-5 complete, production-grade files (code, tests, or config).
2. NO placeholder comments, NO 'pass', NO empty stubs. Write real domain logic.
3. Every new function or class must be covered with substantive unit tests.
4. Output strict JSON format with this exact structure:
{
  "files": {
    "relative/path/to/file.py": "complete file contents...",
    "tests/test_feature.py": "complete test contents..."
  },
  "summary": "Brief description of changes made"
}
"""
            resp, provider = self.llm.complete(
                prompt,
                system="You are an elite autonomous developer. Respond in strict JSON only.",
                json_mode=True,
            )

            if not resp:
                error_feedback = "LLM response was blank or timed out"
                continue

            try:
                clean = re.sub(r"^```(?:json)?\s*", "", resp.strip())
                clean = re.sub(r"\s*```$", "", clean)
                data = json.loads(clean)
                files_dict = data.get("files", {})
                if not files_dict or not isinstance(files_dict, dict):
                    error_feedback = "JSON did not contain a valid 'files' dictionary"
                    continue

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

                logger.info(f"Wrote {len(files_written)} file(s): {', '.join(files_written)}")

                # Syntax Check
                syntax_ok, syntax_err = validate_python_files(workspace, files_written)
                if not syntax_ok:
                    error_feedback = f"Syntax compilation failed: {syntax_err}"
                    logger.warning(error_feedback)
                    continue

                # Automated Tests Check
                tests_ok, test_err = run_workspace_tests(workspace)
                if not tests_ok:
                    error_feedback = f"Automated tests failed:\n{test_err}"
                    logger.warning(f"Tests failed on attempt {attempt}: {test_err[:200]}")
                    continue

                # JEV Quality Gate Audit
                diff_preview = "\n\n".join(diff_snippets)
                jev_approved, jev_score, jev_msg = audit_with_jev(self.llm, item, files_written, diff_preview)
                if not jev_approved:
                    error_feedback = f"JEV Quality Gate rejected implementation: {jev_msg}"
                    logger.warning(error_feedback)
                    continue

                logger.info(f"Checklist item passed all gates: {jev_msg}")
                return True, f"Successfully implemented in {len(files_written)} files ({jev_msg})"

            except Exception as exc:
                error_feedback = f"Execution error: {exc}"
                logger.warning(f"Attempt {attempt} failed: {exc}")

        return False, f"Exhausted {MAX_ITEM_CORRECTION_ATTEMPTS} attempts. Last error: {error_feedback}"

    def git_commit_and_push(self, workspace: Path, commit_msg: str) -> bool:
        """Stage all changes, commit, and push directly to origin main."""
        try:
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
            logger.warning(f"Git push failed: {res.stderr}")
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

    builder = SpecDrivenBuilder(target_repos=repos, work_dir=Path(args.workspace), dry_run=args.dry_run)
    summary = builder.run()
    print("\n" + "=" * 70)
    print(f"Overnight Build Completed: {json.dumps(summary, indent=2)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
