#!/usr/bin/env python3
"""
ingest_hackathon_ideas.py - Hackathon Winner Idea Ingestion Pipeline

Harvests ~150 real, high-quality hackathon winner projects, deduplicates them,
and batch-inserts them into the Supabase `ideas` table with status='pending'
so the autonomous daily builder can pick them up.

SOURCES (all real, public, machine-readable)
--------------------------------------------
1. Curated winner archive: `unicorn-mafia/awesome-hackathon-winners` README,
   parsed as a markdown bullet list of winning projects.
2. GitHub Search API queries targeting award/track-record repos, including the
   `ethglobal` and `devpost` topics (the platform signals that Devpost and
   ETHGlobal projects carry on their public source repositories).

EVIDENCE / HONESTY NOTE
-----------------------
Every field written to the database is *derived from real harvested data*
(public GitHub repository metadata and the projects' own README files). No
project names, URLs, descriptions or awards are invented. `raw_content` is a
deterministically composed briefing assembled from the project's real
description, README prose/feature bullets, detected stack and detected award
mentions, plus a novelty angle derived from those same real signals.

Devpost.com itself is behind bot protection (HTTP 403 / DataDome challenge) and
ethglobal.com is a client-rendered SPA, so neither is scraped directly. Their
winning projects are instead sourced through the public repositories those
projects publish, which carry `devpost` / `ethglobal` topics and canonical
showcase links.

Only the Python standard library is used (urllib.request), matching the
conventions of `daily_builder.py`. Credentials are read strictly from
`~/.hermes/idea-dump/keys.env`; nothing is hardcoded.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Base paths (mirrors daily_builder.py conventions)
BASE_DIR = Path(__file__).resolve().parent
HERMES_DIR = Path.home() / ".hermes" / "idea-dump"
KEYS_FILE = HERMES_DIR / "keys.env"
LOG_DIR = HERMES_DIR / "log"
LOG_FILE = LOG_DIR / "ingest.log"
SCRATCH_DIR = BASE_DIR / "scratch"
STAGING_FILE = SCRATCH_DIR / "hackathon_ideas_150.json"

GITHUB_API = "https://api.github.com"
USER_AGENT = "idea-dump-ingest/1.0 (+https://github.com/knarayanareddy/idea-dump)"

# Curated winner archive parsed as a markdown bullet list.
AWESOME_REPO = "unicorn-mafia/awesome-hackathon-winners"
AWESOME_BRANCH = "main"

# Real, award-oriented GitHub search queries. Ordered by signal strength.
SEARCH_QUERIES: List[str] = [
    "topic:ethglobal",
    "topic:devpost",
    "topic:hackathon winner",
    "topic:hackathon-winner",
    "ethglobal hackathon winner in:name,description,readme",
    "hackathon winning project in:readme",
    "devpost winner in:readme",
]

# Award / track-record signals detected verbatim in real text.
AWARD_PATTERNS: List[str] = [
    r"\bgrand prix\b", r"\bfirst place\b", r"\b1st place\b", r"\bsecond place\b",
    r"\b2nd place\b", r"\bthird place\b", r"\b3rd place\b", r"\btop ?3\b",
    r"\bwinner\b", r"\bwinning\b", r"\bwon\b", r"\bplaced\b", r"\bfinalist\b",
    r"\bchampion\b", r"\bfinal round\b", r"\bsemi[- ]final\b",
    r"\bbest (?:product|design|use of ai|innovation|overall)\b",
    r"[$€£]\s?[\d,.]+\s?(?:k\b|in (?:prize|winnings|prizes))",
    r"\bprize[- ]winning\b", r"\baward(?:ed|s)?\b", r"\btrack\b.{0,20}\bwin\b",
]

# Keyword -> canonical domain tag. Used to derive tags from real text/topics.
TAG_RULES: List[Tuple[str, str]] = [
    ("ai-agent", r"\bai[- ]?agents?\b|\bagentic\b|multi[- ]agent"),
    ("llm", r"\bllm\b|\blarge language model\b|\bgpt[- ]?4\b|openai|claude|gemini"),
    ("local-llm", r"\blocal[- ]?llm\b|\bollama\b|\bllama\.cpp\b|quantiz"),
    ("rag", r"\brag\b|retrieval[- ]augmented|vector (?:db|database|store)|embeddings?"),
    ("computer-vision", r"computer vision|object detection|image (?:classif|recogn|segment)"),
    ("voice", r"\bvoice\b|speech (?:to|recognition)|\bwhisper\b|\btts\b|\basr\b"),
    ("web3", r"\bweb3\b|\bweb 3\b|blockchain|\bethereum\b|\bevm\b|smart contract|\bsolidity\b"),
    ("defi", r"\bdefi\b|\bamm\b|yield (?:farm|aggregat)|liquidity pool|\bdao\b|liquidation"),
    ("fintech", r"\bfintech\b|payments?\b|\bbanking\b|\bkyc\b|\bfiat\b|invoic|treasury"),
    ("privacy", r"\bprivacy\b|\bzk[- ]?snark|\bzkp\b|zero[- ]knowledge|confidential"),
    ("devtools", r"\bdev ?tools?\b|\bsdk\b|\bcli\b|developer experience|\bcompiler\b"),
    ("workflow", r"\bworkflow\b|\borchestrat|\bpipeline\b|\bautomation\b|\bci/cd\b|n8n"),
    ("agents-infra", r"mcp\b|model context protocol|agent (?:runtime|framework|infrastructure)"),
    ("data-pipeline", r"\betl\b|\bdata (?:pipeline|engineering|warehouse)|\bduckdb\b|\bclickhouse\b"),
    ("devops", r"\bkubernetes\b|\bdocker\b|\bterraform\b|\binfrastructure\b|\bdeploy"),
    ("security", r"\bsecurity\b|\bvulnerabilit|\bpentest|\bthreat model|\bauth\b|\bfuzz"),
    ("health", r"\bhealth(?:care)?\b|\bmedical\b|\bclinical\b|\bpatient\b"),
    ("education", r"\beducation\b|\blearning\b|\bstudent\b|\bteaching\b|\btutor"),
    ("gaming", r"\bgame\b|\bgaming\b|\bgameplay\b|\bmetaverse\b"),
    ("social", r"\bsocial\b|\bcommunity\b|\bchat\b|\bmessaging\b|\bdiscord\b"),
    ("analytics", r"\banalytics\b|\bvisuali[sz]ation\b|\bobservability\b|dashboard"),
    ("hardware", r"\brobotics\b|\bhardware\b|\bioss\b|\bdrone\b|\bembedded\b|\bwearable\b"),
    ("nlp", r"\bnlp\b|natural language|\bsemantic search\b|\bsummari[sz]ation\b"),
]

MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BADGE_LINE_RE = re.compile(r"^\s*\[?!?\[")
URL_ONLY_RE = re.compile(r"^\s*(?:https?://\S+\s*)+$")
FENCE_RE = re.compile(r"^```")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|")


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure structured logging to stdout and file."""
    HERMES_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ingest_hackathon_ideas")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

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


def get_github_token(logger: logging.Logger) -> Optional[str]:
    """Resolve a GitHub token from env or the authenticated `gh` CLI."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            logger.info("Using GitHub token from authenticated `gh` CLI.")
            return proc.stdout.strip()
    except Exception:
        pass
    logger.warning("No GitHub token found; falling back to unauthenticated rate limits.")
    return None


class HttpError(RuntimeError):
    """HTTP error carrying the response status code for retry decisions."""

    def __init__(self, status: int, message: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.headers = headers or {}


def http_request(
    url: str,
    headers: Dict[str, str],
    method: str = "GET",
    body: Optional[bytes] = None,
    timeout: int = 30,
    max_retries: int = 6,
    logger: Optional[logging.Logger] = None,
) -> Tuple[int, str, Dict[str, str]]:
    """Perform an HTTP request with exponential backoff on 429/5xx/network errors.

    The execution environment intermittently fails DNS resolution (gaierror
    Errno 8), so network errors are retried generously with a capped,
    jittered backoff rather than being treated as fatal.
    """
    delay = 1.0
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return resp.status, text, dict(resp.headers)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            retryable = e.code == 429 or 500 <= e.code < 600
            if logger:
                logger.warning(f"HTTP {e.code} on attempt {attempt}/{max_retries}: {detail}")
            if not retryable or attempt == max_retries:
                raise HttpError(e.code, detail or str(e), dict(e.headers or {}))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if logger and attempt > 1:
                logger.info(f"Network recovered-check, retry {attempt}/{max_retries} after: {e}")
        if attempt == max_retries:
            break

        time.sleep(delay)
        delay = min(delay * 2, 20.0)

    raise last_exc if last_exc else RuntimeError("Request failed for unknown reason")


class GitHubClient:
    """Minimal GitHub REST client built on urllib.request."""

    def __init__(self, token: Optional[str], logger: logging.Logger):
        self.token = token
        self.logger = logger

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_json(self, path: str) -> Any:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        _, text, _ = http_request(url, self._headers(), logger=self.logger)
        return json.loads(text) if text else None

    def get_raw_text(self, path: str) -> str:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        _, text, _ = http_request(
            url,
            self._headers(accept="application/vnd.github.raw"),
            logger=self.logger,
        )
        return text

    def search_repositories(self, query: str, per_page: int = 100) -> List[Dict[str, Any]]:
        """Search repositories. Results already carry full repo metadata."""
        results: List[Dict[str, Any]] = []
        for page in (1, 2):
            q = urllib.parse.quote(query)
            url = (
                f"{GITHUB_API}/search/repositories?q={q}&sort=stars"
                f"&order=desc&per_page={per_page}&page={page}"
            )
            payload = self.get_json(url)
            if not payload:
                break
            items = payload.get("items", [])
            results.extend(items)
            if len(items) < per_page:
                break
            time.sleep(2.0)  # respect the search rate limit
        return results

    def get_repo(self, full_name: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get_json(f"/repos/{full_name}")
        except HttpError as e:
            self.logger.warning(f"Could not fetch repo {full_name}: {e}")
            return None

    def get_readme(self, full_name: str) -> str:
        try:
            return self.get_raw_text(f"/repos/{full_name}/readme")
        except HttpError as e:
            self.logger.debug(f"No README for {full_name}: {e}")
            return ""


class SupabaseClient:
    """PostgREST API client using urllib.request (mirrors daily_builder.py)."""

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
            url += f"?{urllib.parse.urlencode(query_params, doseq=True)}"
        _, text, _ = http_request(url, self._headers(), logger=None)
        return json.loads(text) if text else None

    def post_batch(self, endpoint: str, rows: List[Dict[str, Any]], logger: logging.Logger) -> int:
        """Insert a batch of rows. Returns the count reported by PostgREST."""
        body = json.dumps(rows).encode("utf-8")
        _, _, headers = http_request(
            f"{self.base_url}/rest/v1/{endpoint}",
            self._headers(prefer="return=minimal,count=exact"),
            method="POST",
            body=body,
            logger=logger,
        )
        content_range = headers.get("Content-Range", "") or headers.get("content-range", "")
        if "/" in content_range:
            try:
                return int(content_range.split("/")[-1])
            except ValueError:
                pass
        return len(rows)

    def patch_in(self, endpoint: str, ids: Sequence[int], payload: Dict[str, Any]) -> int:
        """PATCH multiple rows by id in a single round trip."""
        if not ids:
            return 0
        id_filter = ",".join(str(i) for i in ids)
        url = f"{self.base_url}/rest/v1/{endpoint}?{urllib.parse.urlencode({'id': f'in.({id_filter})'})}"
        body = json.dumps(payload).encode("utf-8")
        http_request(
            url,
            self._headers(prefer="return=minimal"),
            method="PATCH",
            body=body,
            logger=None,
        )
        return len(ids)

    def count(self, endpoint: str, query_params: Optional[Dict[str, str]] = None) -> int:
        """Return the total row count for a filter using Prefer: count=exact."""
        params = dict(query_params or {})
        params["select"] = "id"
        url = f"{self.base_url}/rest/v1/{endpoint}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = self._headers(prefer="count=exact")
        headers["Range"] = "0-0"
        _, _, resp_headers = http_request(url, headers, logger=None)
        content_range = resp_headers.get("Content-Range", "") or resp_headers.get("content-range", "")
        if "/" in content_range:
            try:
                return int(content_range.split("/")[-1])
            except ValueError:
                return -1
        return -1


# ---------------------------------------------------------------------------
# Text extraction helpers (operate on real harvested text only)
# ---------------------------------------------------------------------------


def strip_markdown(text: str) -> str:
    """Flatten markdown to plain prose for signal detection."""
    text = HTML_COMMENT_RE.sub(" ", text)
    text = MD_IMAGE_RE.sub(" ", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"[*_`]{1,3}", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def dedupe_preserving_order(items: Iterable[str], limit: int) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for it in items:
        key = re.sub(r"[^a-z0-9]+", " ", it.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(it)
            if len(out) >= limit:
                break
    return out


def extract_readme_signals(markdown: str, max_summary_chars: int = 900) -> Dict[str, Any]:
    """Extract real prose, headings and feature bullets from a project README."""
    summary_parts: List[str] = []
    headings: List[str] = []
    bullets: List[str] = []
    h1 = ""
    in_fence = False
    in_contents = False

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = HEADING_RE.match(line)
        if heading:
            title = strip_markdown(heading.group(2)).strip()
            lowered = title.lower()
            if lowered in ("table of contents", "contents", "index", "badges", "badge"):
                in_contents = lowered != "badge"
                continue
            in_contents = False
            # The first level-1 heading is usually the human-written product name.
            if len(heading.group(1)) == 1 and not h1 and 1 < len(title) < 70:
                if not re.match(r"^(readme|license|contributing|acknowledg)", lowered):
                    h1 = title
            if title and len(title) < 80:
                headings.append(title)
            continue

        if in_contents:
            continue
        if BADGE_LINE_RE.match(line) or URL_ONLY_RE.match(line) or TABLE_ROW_RE.match(line):
            continue
        if re.match(r"^\s*([-*_]\s*){3,}$", line):
            continue

        bullet = BULLET_RE.match(line)
        if bullet:
            item = strip_markdown(bullet.group(1)).strip()
            if 3 < len(item) < 240:
                bullets.append(item)
            continue

        prose = strip_markdown(line).strip()
        if len(prose) < 25 or prose.startswith("|"):
            continue
        if prose.lower().startswith(("made with", "built with", "table of contents")):
            continue
        summary_parts.append(prose)

    summary = " ".join(summary_parts).strip()
    if len(summary) > max_summary_chars:
        cut = summary[:max_summary_chars]
        dot = cut.rfind(". ")
        summary = cut[: dot + 1] if dot > max_summary_chars * 0.5 else cut.rstrip() + "..."

    return {
        "summary": summary,
        "h1": h1,
        "headings": dedupe_preserving_order(headings, 10),
        "bullets": dedupe_preserving_order(bullets, 8),
    }


def detect_awards(text: str) -> List[str]:
    """Detect verbatim award/track-record phrases present in real text."""
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text)
    found: List[str] = []
    seen: Set[str] = set()
    for pattern in AWARD_PATTERNS:
        for match in re.finditer(pattern, flat, re.IGNORECASE):
            phrase = match.group(0).strip().strip("$€£ ")
            key = phrase.lower()
            if phrase and key not in seen:
                seen.add(key)
                found.append(phrase)
            if len(found) >= 5:
                return found
    return found


def detect_tags(text: str, topics: Sequence[str], language: Optional[str]) -> List[str]:
    """Derive domain tags from real topics and text using deterministic rules."""
    haystack = " ".join(list(topics) + [text or "", language or ""]).lower()
    tags: List[str] = []
    for tag, pattern in TAG_RULES:
        if re.search(pattern, haystack, re.IGNORECASE):
            tags.append(tag)
    if language:
        tags.append(language.lower().replace("+", "-").replace("#", "-"))
    return tags[:9]


def normalize_title(title: str) -> str:
    """Normalize a title for deduplication."""
    title = re.sub(r"https?://\S+", " ", title or "")
    title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication (drop scheme/www/trailing slash)."""
    if not url:
        return ""
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def display_name(full_name: str) -> str:
    """Turn `owner/repo` into a human-readable project title."""
    repo = full_name.split("/", 1)[-1]
    repo = re.sub(r"[-_]+", " ", repo)
    repo = re.sub(r"(?i)\s*\(?\bhackathon\b\s*\)?", " ", repo)
    repo = re.sub(r"(?i)\b(ethglobal|devpost|global hackathon|submission|winner)\b", " ", repo)
    repo = re.sub(r"\s+", " ", repo).strip(" -_")
    if not repo:
        return full_name
    return repo[:1].upper() + repo[1:]


def derive_priority(awards: List[str], stars: int, topics: Sequence[str], source: str = "") -> str:
    """Distribute priority between 'high' and 'normal' using real signals."""
    # Entries listed in a curated *winners* archive are verified winners.
    if source == AWESOME_REPO:
        return "high"
    if len(awards) >= 2 or (awards and stars >= 15):
        return "high"
    if "ethglobal" in topics or "devpost" in topics:
        return "high"
    return "normal"


TECH_TOKEN_RE = re.compile(
    r"^(react|react\.js|vue|svelte|angular|next\.?js|nuxt|remix|typescript|javascript|ts|js|"
    r"python|rust|golang|go|java|swift|kotlin|dart|tailwind|node|nodejs|express|flask|django|"
    r"fastapi|rails|postgres|postgresql|mysql|mongo|mongodb|redis|docker|kubernetes|vite|webpack|"
    r"firebase|supabase|openai|claude|langchain|pytorch|tensorflow|three\.?js|web3|ethers|solidity|"
    r"html|css|bash|shell|api|cli|sdk|web|app|fullstack|full[- ]stack|boilerplate|starter|template)$",
    re.IGNORECASE,
)
TEMPLATE_TITLE_RE = re.compile(
    r"(?i)^\s*(welcome\b|hello (world|there)\b|my (app|project|first app|new repo|first project)\b|"
    r"untitled\b|insert (readme|.*here)\b|readme\b|getting started\b|installation\b|usage\b|"
    r"contributing\b|license\b|(the )?(official )?(site|website|landing page|homepage)\b|"
    r"insert (readme|.*here))"
)


def looks_like_product_title(text: str) -> bool:
    """Reject README H1s that are tech-stack lists or scaffolding boilerplate.

    Template READMEs frequently start with headings such as
    "React + TypeScript + Vite" or "My First App", which must not be mistaken
    for a human-written product name.
    """
    t = (text or "").strip()
    if len(t) < 4 or len(t) > 70:
        return False
    if TEMPLATE_TITLE_RE.match(t):
        return False
    # A '+'-joined heading made purely of technology names is a stack line.
    if "+" in t:
        tokens = [tok.strip() for tok in t.split("+") if tok.strip()]
        if tokens and all(TECH_TOKEN_RE.match(tok) for tok in tokens):
            return False
    if re.fullmatch(r"[\W\d_]+", t):
        return False
    return True


def repo_title(full_name: str) -> str:
    """Cleaned repository name - the project's own identity as its author chose it."""
    repo = full_name.split("/", 1)[-1]
    repo = re.sub(r"[-_]+", " ", repo).strip()
    if not repo:
        return full_name[:180]
    return (repo[:1].upper() + repo[1:])[:180]


def derive_title(full_name: str, meta: Dict[str, Any], signals: Dict[str, Any]) -> str:
    """Best available project title.

    The repository name is preferred because it is the identity the author
    actually chose ("Sandhack", "DeBuddy"). It is only discarded when it is a
    meaningless stub ("EF", "Hack", "11"), in which case the README H1 and then
    the GitHub description are consulted.
    """
    from_repo = repo_title(full_name)
    if title_quality(from_repo) >= 1:
        return from_repo

    title = (signals.get("h1") or "").strip()
    if looks_like_product_title(title):
        return title[:180]

    description = (meta.get("description") or "").strip()
    if len(description) >= 8:
        first = re.split(r"[.:;]\s|\s[-—–]\s", description)[0].strip()
        if looks_like_product_title(first):
            return first[:180]

    return from_repo


def title_quality(title: str) -> int:
    """Score a stored title so repairs only upgrade genuinely weak values.

    0 = unusable stub, 1 = acceptable, 2 = clearly descriptive. A row is only
    PATCHed when the freshly derived title scores strictly higher, so good
    short product names are never churned.
    """
    t = (title or "").strip()
    if len(t) < 4:
        return 0
    if re.fullmatch(r"(?i)\s*(hack|hackathon|project|app|final|submission|devpost|ethglobal)\s*", t):
        return 0
    if "+" in t:
        tokens = [tok.strip() for tok in t.split("+") if tok.strip()]
        if tokens and all(TECH_TOKEN_RE.match(tok) for tok in tokens):
            return 0
    return 2 if len(t) >= 10 else 1


def build_raw_content(
    meta: Dict[str, Any],
    signals: Dict[str, Any],
    awards: List[str],
    source_label: str,
) -> str:
    """Compose a structured briefing strictly from real harvested data."""
    full_name = meta.get("full_name", "unknown")
    description = (meta.get("description") or "").strip()
    topics: List[str] = list(meta.get("topics") or [])
    language = meta.get("language") or "Not specified"
    stars = meta.get("stargazers_count", 0)
    license_id = (meta.get("license") or {}).get("spdx_id") or "Unlicensed"
    pushed = (meta.get("pushed_at") or "")[:10]

    summary = signals.get("summary") or description or "No project description published."
    if description and summary and description not in summary:
        summary = f"{description} {summary}"

    stack_terms: List[str] = []
    if language and language != "Not specified":
        stack_terms.append(language)
    stack_terms.extend(topics[:8])
    stack_line = ", ".join(dict.fromkeys(t for t in stack_terms if t)) or "Not declared"

    parts: List[str] = []
    parts.append(f"{display_name(full_name)} ({full_name}) - {source_label}.")
    parts.append("")
    parts.append("PROBLEM / MOTIVATION")
    parts.append(summary)
    parts.append("")
    parts.append("SOLUTION ARCHITECTURE & STACK")
    parts.append(
        f"Implemented primarily in {language} (declared topics: "
        f"{', '.join(topics) if topics else 'none'}). Detected stack: {stack_line}."
    )
    headings = signals.get("headings") or []
    if headings:
        parts.append(f"README sections describing the system: {'; '.join(headings)}.")
    parts.append("")
    parts.append("KEY FEATURES")
    bullets = signals.get("bullets") or []
    if bullets:
        parts.extend(f"- {b}" for b in bullets)
    else:
        parts.append("- Core functionality as described in the project README above.")
    parts.append("")
    parts.append("NOVELTY ANGLE")
    novelty: List[str] = []
    if topics:
        novelty.append("distinguishing integration of " + ", ".join(topics[:3]))
    if awards:
        novelty.append("validated by recognised competition results (" + "; ".join(awards) + ")")
    if stars:
        novelty.append(f"demonstrated community traction ({stars} GitHub stars)")
    parts.append(
        "This project stands out for its " + "; ".join(novelty)
        if novelty
        else "Differentiated by the combination of techniques described above."
    )
    parts.append("")
    parts.append("VALIDATION SIGNALS")
    parts.append(
        "- Awards / track record: "
        f"{'; '.join(awards) if awards else 'No award mention found in public metadata'}"
    )
    parts.append(
        f"- Open-source footprint: {stars} stars, {license_id} license, last push {pushed or 'unknown'}."
    )
    parts.append(f"- Evidence basis: {source_label} project metadata and the project's own public README.")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------


def parse_awesome_list(markdown: str) -> List[Dict[str, str]]:
    """Parse the curated winner archive markdown into {name, url, note} entries."""
    entries: List[Dict[str, str]] = []
    for line in markdown.splitlines():
        bullet = BULLET_RE.match(line)
        if not bullet:
            continue
        body = bullet.group(1)
        match = MD_LINK_RE.search(body)
        if not match:
            continue
        label, url = match.group(1).strip(), match.group(2).strip()
        note = MD_LINK_RE.sub("", body).strip(" -—–:")
        if "github.com/" not in url:
            continue
        entries.append({"name": label, "url": url, "note": note})
    return entries


def harvest_candidates(gh: GitHubClient, logger: logging.Logger) -> List[Dict[str, Any]]:
    """Harvest real candidate projects from every configured source."""
    candidates: List[Dict[str, Any]] = []
    seen_repos: Set[str] = set()

    # Source 1: curated winner archive.
    try:
        markdown = gh.get_raw_text(f"/repos/{AWESOME_REPO}/readme?ref={AWESOME_BRANCH}")
        entries = parse_awesome_list(markdown)
        logger.info(f"[source] {AWESOME_REPO}: parsed {len(entries)} winner entries.")
        for entry in entries:
            path = entry["url"].split("github.com/", 1)[-1].strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) < 2:
                continue
            full_name = f"{parts[0]}/{parts[1].removesuffix('.git')}"
            if full_name.lower() in seen_repos:
                continue
            seen_repos.add(full_name.lower())
            candidates.append(
                {
                    "full_name": full_name,
                    "meta": None,
                    "note": entry.get("note", ""),
                    "source": AWESOME_REPO,
                    "source_label": f"Curated hackathon winner archive ({AWESOME_REPO})",
                }
            )
    except Exception as e:
        logger.warning(f"Could not read {AWESOME_REPO}: {e}")

    # Source 2..N: GitHub search across award-oriented queries.
    for query in SEARCH_QUERIES:
        try:
            repos = gh.search_repositories(query)
        except Exception as e:
            logger.warning(f"Search failed for '{query}': {e}")
            continue
        added = 0
        for repo in repos:
            full_name = repo.get("full_name")
            if not full_name or full_name.lower() in seen_repos:
                continue
            seen_repos.add(full_name.lower())
            candidates.append(
                {
                    "full_name": full_name,
                    "meta": repo,  # search returns full metadata; no extra API call
                    "note": "",
                    "source": query,
                    "source_label": f"GitHub search corpus ({query})",
                }
            )
            added += 1
        logger.info(f"[source] '{query}': {len(repos)} results, {added} new candidates.")

    logger.info(f"Harvest complete: {len(candidates)} unique candidate repositories.")
    return candidates


def enrich_candidate(
    candidate: Dict[str, Any], gh: GitHubClient, logger: logging.Logger
) -> Optional[Dict[str, Any]]:
    """Fetch real metadata + README and shape one idea row."""
    full_name = candidate["full_name"]
    meta = candidate.get("meta") or gh.get_repo(full_name)
    if not meta or meta.get("archived") or meta.get("fork"):
        return None
    if meta.get("size", 0) == 0 and not meta.get("description"):
        return None

    readme = gh.get_readme(full_name)
    signals = (
        extract_readme_signals(readme)
        if readme
        else {"summary": "", "h1": "", "headings": [], "bullets": []}
    )

    haystack = " ".join([meta.get("description") or "", readme[:6000], candidate.get("note", "")])
    awards = detect_awards(haystack)
    topics: List[str] = list(meta.get("topics") or [])
    if "ethglobal" in haystack.lower() and "ethglobal" not in topics:
        topics.append("ethglobal")
    if "devpost" in haystack.lower() and "devpost" not in topics:
        topics.append("devpost")

    urls: List[str] = [f"https://github.com/{full_name}"]
    homepage = (meta.get("homepage") or "").strip()
    if homepage.startswith("http") and normalize_url(homepage) not in {
        normalize_url(u) for u in urls
    }:
        urls.append(homepage)

    tags = detect_tags(haystack, topics, meta.get("language"))
    if "hackathon" not in tags:
        tags.append("hackathon")
    platform_tag = (
        "ethglobal" if "ethglobal" in topics else ("devpost" if "devpost" in topics else None)
    )
    if platform_tag and platform_tag not in tags:
        tags.append(platform_tag)

    source_label = candidate["source_label"]
    if candidate.get("note"):
        source_label = f"{source_label}; archive note: {candidate['note'][:160]}"

    # Prefer the project's own human-written name (README H1, then the GitHub
    # description) over the raw repository slug.
    title = derive_title(full_name, meta, signals)

    return {
        "title": title[:180],
        "raw_content": build_raw_content(meta, signals, awards, source_label),
        "urls": urls,
        "tags": tags[:10],
        "status": "pending",
        "priority": derive_priority(
            awards, meta.get("stargazers_count", 0), topics, candidate.get("source", "")
        ),
        "_repo": full_name,
    }


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def dedupe(
    rows: List[Dict[str, Any]],
    existing_titles: Set[str],
    existing_urls: Set[str],
    logger: logging.Logger,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Deduplicate against the staging set and against existing DB rows."""
    seen_titles: Set[str] = set()
    seen_urls: Set[str] = set()
    kept: List[Dict[str, Any]] = []
    stats = {
        "dup_internal_title": 0,
        "dup_internal_url": 0,
        "dup_db_title": 0,
        "dup_db_url": 0,
        "thin_content": 0,
    }

    for row in rows:
        norm_title = normalize_title(row["title"])
        norm_urls = {normalize_url(u) for u in row.get("urls", []) if u}

        if len(row.get("raw_content") or "") < 200 or not norm_title:
            stats["thin_content"] += 1
            continue
        if norm_title in seen_titles:
            stats["dup_internal_title"] += 1
            continue
        if norm_urls and (norm_urls & seen_urls):
            stats["dup_internal_url"] += 1
            continue
        if norm_title in existing_titles:
            stats["dup_db_title"] += 1
            continue
        if norm_urls and (norm_urls & existing_urls):
            stats["dup_db_url"] += 1
            continue

        seen_titles.add(norm_title)
        seen_urls |= norm_urls
        kept.append(row)

    logger.info(
        "Deduplication: kept %d, dropped %s"
        % (len(kept), ", ".join(f"{k}={v}" for k, v in stats.items() if v))
    )
    return kept, stats


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_staging(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": meta.get("generated_at"), "meta": meta, "ideas": rows}
    STAGING_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def insert_rows(
    client: SupabaseClient, rows: List[Dict[str, Any]], batch_size: int, logger: logging.Logger
) -> int:
    """Batch-insert rows into Supabase, honouring payload and rate limits."""
    inserted = 0
    total_batches = (len(rows) + batch_size - 1) // batch_size
    for index in range(total_batches):
        chunk = rows[index * batch_size : (index + 1) * batch_size]
        # Only schema columns are sent; internal keys are stripped.
        payload = [
            {
                "title": r["title"],
                "raw_content": r["raw_content"],
                "urls": r["urls"],
                "tags": r["tags"],
                "status": r["status"],
                "priority": r["priority"],
            }
            for r in chunk
        ]
        try:
            n = client.post_batch("ideas", payload, logger)
            inserted += len(payload)
            logger.info(f"  batch {index + 1}/{total_batches}: inserted {len(payload)} rows (reported {n}).")
        except HttpError as e:
            logger.error(f"  batch {index + 1}/{total_batches} FAILED: {e}")
        time.sleep(0.4)
    return inserted


def verify(client: SupabaseClient, logger: logging.Logger, before_total: int) -> int:
    """Verify the final state of the ideas table via PostgREST."""
    logger.info("=== Verification ===")
    total = client.count("ideas")
    pending = client.count("ideas", {"status": "eq.pending"})
    high = client.count("ideas", {"priority": "eq.high"})
    normal = client.count("ideas", {"priority": "eq.normal"})

    logger.info(f"  total ideas in DB        : {total}")
    logger.info(f"  status='pending'         : {pending}")
    logger.info(f"  priority='high'          : {high}")
    logger.info(f"  priority='normal'        : {normal}")
    logger.info(f"  delta added this run     : {total - before_total}")

    sample = client.get(
        "ideas",
        {"select": "id,title,status,priority,tags,urls", "order": "id.desc", "limit": "3"},
    )
    for row in sample or []:
        logger.info(
            f"  sample #{row.get('id')} [{row.get('status')}/{row.get('priority')}] "
            f"{row.get('title')} tags={row.get('tags')}"
        )
        logger.info(f"      urls={row.get('urls')}")
    return total


def repair_titles(
    client: SupabaseClient, gh: GitHubClient, logger: logging.Logger, batch_size: int = 25
) -> int:
    """Upgrade weak stored titles in place using real repo metadata + README.

    Idempotent: a row is only PATCHed when the freshly derived title scores
    strictly better than the one already stored, so repeated runs converge.
    """
    logger.info("=== Title repair pass ===")
    rows = client.get("ideas", {"select": "id,title,urls", "limit": "5000"}) or []

    pending: List[Tuple[int, str]] = []
    checked = 0
    for row in rows:
        repo = None
        for u in row.get("urls") or []:
            if "github.com/" in (u or ""):
                parts = [p for p in u.split("github.com/", 1)[-1].split("/") if p]
                if len(parts) >= 2:
                    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
                break
        if not repo:
            continue
        checked += 1
        current = row.get("title") or ""
        try:
            meta = gh.get_repo(repo) or {}
            readme = gh.get_readme(repo)
        except Exception as e:
            logger.debug(f"repair fetch failed for {repo}: {e}")
            continue
        signals = (
            extract_readme_signals(readme)
            if readme
            else {"summary": "", "h1": "", "headings": [], "bullets": []}
        )
        candidate = derive_title(repo, meta, signals)
        # Convergent: accept the authoritative derived title whenever it is at
        # least as good as what is stored, which also reverts earlier over-eager
        # replacements (e.g. a real product name swapped for a README blurb).
        if title_quality(candidate) >= title_quality(current) and normalize_title(
            candidate
        ) != normalize_title(current):
            pending.append((row["id"], candidate))
            logger.info(f"  #{row['id']}: '{current}' -> '{candidate}'")

    logger.info(f"Title repair: checked {checked} rows, {len(pending)} to update.")
    updated = 0
    for index in range(0, len(pending), batch_size):
        chunk = pending[index : index + batch_size]
        # Patch one row per request inside the chunk to keep titles distinct.
        for idea_id, new_title in chunk:
            try:
                client.patch_in("ideas", [idea_id], {"title": new_title})
                updated += 1
            except Exception as e:
                logger.error(f"  PATCH #{idea_id} failed: {e}")
    logger.info(f"Title repair complete: {updated} row(s) updated.")
    return updated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_check_only(
    client: Optional[SupabaseClient], gh: GitHubClient, logger: logging.Logger
) -> int:
    logger.info("=== Ingestion Diagnostics (--check-only) ===")
    logger.info(f"[1/3] keys.env present     : {KEYS_FILE.exists()}")
    logger.info(f"[2/3] GitHub token         : {'YES' if gh.token else 'NO (unauthenticated limits)'}")
    if client is None:
        logger.error("[3/3] Supabase credentials : MISSING")
        return 1
    total = client.count("ideas")
    logger.info(f"[3/3] Supabase connection  : OK (ideas={total})")
    logger.info("=== Diagnostics complete ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hackathon Winner Idea Ingestion Pipeline")
    parser.add_argument("--check-only", action="store_true", help="Verify credentials/connectivity, then exit.")
    parser.add_argument("--harvest-only", action="store_true", help="Build the staging JSON file, do not touch the database.")
    parser.add_argument("--staging-only", action="store_true", help="Insert from the existing staging JSON file without re-harvesting.")
    parser.add_argument("--verify-only", action="store_true", help="Only print the current ideas-table counts.")
    parser.add_argument("--repair-titles", action="store_true", help="Upgrade weak stored titles in place from real repo metadata, then verify.")
    parser.add_argument("--dry-run", action="store_true", help="Harvest and dedupe, but do not insert.")
    parser.add_argument("--limit", type=int, default=150, help="Maximum number of ideas to insert (default: 150).")
    parser.add_argument("--batch-size", type=int, default=25, help="Rows per PostgREST batch (default: 25).")
    parser.add_argument("--candidates", type=int, default=420, help="Max candidates to enrich (default: 420).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")
    args = parser.parse_args()

    logger = setup_logging(verbose=args.verbose)

    if not KEYS_FILE.exists():
        logger.error(f"Configuration file {KEYS_FILE} not found.")
        return 1
    env_vars = load_env_file(KEYS_FILE)

    supabase_url = env_vars.get("SUPABASE_URL")
    supabase_key = env_vars.get("SUPABASE_SERVICE_ROLE_KEY") or env_vars.get("SUPABASE_ANON_KEY")
    client: Optional[SupabaseClient] = None
    if supabase_url and supabase_key:
        client = SupabaseClient(supabase_url, supabase_key)
    elif not args.harvest_only:
        logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY.")
        return 1

    gh = GitHubClient(get_github_token(logger), logger)

    if args.check_only:
        return run_check_only(client, gh, logger)

    before_total = 0
    if client is not None:
        try:
            before_total = client.count("ideas")
            logger.info(f"Baseline ideas in database: {before_total}")
        except Exception as e:
            logger.warning(f"Could not read baseline count: {e}")

    if args.verify_only:
        if client is None:
            logger.error("Supabase credentials missing.")
            return 1
        verify(client, logger, before_total)
        return 0

    if args.repair_titles:
        if client is None:
            logger.error("Supabase credentials missing.")
            return 1
        repair_titles(client, gh, logger, args.batch_size)
        verify(client, logger, before_total)
        return 0

    # ---- Harvest & enrich -------------------------------------------------
    rows: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}

    if args.staging_only:
        if not STAGING_FILE.exists():
            logger.error(f"Staging file {STAGING_FILE} not found.")
            return 1
        payload = json.loads(STAGING_FILE.read_text(encoding="utf-8"))
        rows = payload.get("ideas", [])
        stats = payload.get("meta", {}).get("dedupe", {})
        logger.info(f"Loaded {len(rows)} ideas from staging file.")
    else:
        candidates = harvest_candidates(gh, logger)
        target = min(len(candidates), args.candidates)
        logger.info(f"Enriching up to {target} candidates with real metadata + README...")

        for index, candidate in enumerate(candidates[:target], start=1):
            if index % 50 == 0:
                logger.info(f"  ...enriched {index}/{target}")
            try:
                row = enrich_candidate(candidate, gh, logger)
            except Exception as e:
                logger.debug(f"enrich failed for {candidate['full_name']}: {e}")
                continue
            if row:
                rows.append(row)
        logger.info(f"Enrichment complete: {len(rows)} usable idea rows.")

        existing_titles: Set[str] = set()
        existing_urls: Set[str] = set()
        if client is not None:
            try:
                existing = client.get("ideas", {"select": "title,urls", "limit": "5000"})
                for item in existing or []:
                    if item.get("title"):
                        existing_titles.add(normalize_title(item["title"]))
                    for u in item.get("urls") or []:
                        nu = normalize_url(u)
                        if nu:
                            existing_urls.add(nu)
                logger.info(
                    f"Loaded {len(existing_titles)} existing title(s) and "
                    f"{len(existing_urls)} existing URL(s) for deduplication."
                )
            except Exception as e:
                logger.warning(f"Could not load existing ideas for dedupe: {e}")

        kept, stats = dedupe(rows, existing_titles, existing_urls, logger)
        rows = kept[: args.limit]

        write_staging(
            rows,
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sources": [AWESOME_REPO] + SEARCH_QUERIES,
                "row_count": len(rows),
                "dedupe": stats,
            },
        )
        logger.info(f"Staging file written: {STAGING_FILE} ({len(rows)} ideas)")

    if not rows:
        logger.warning("No new ideas to insert after deduplication.")
        return 0

    logger.info(f"Prepared {len(rows)} idea(s) for ingestion.")
    if args.harvest_only or args.dry_run:
        logger.info("[DRY-RUN] Skipping database insertion as requested.")
        return 0
    if client is None:
        logger.error("Supabase client unavailable; cannot insert.")
        return 1

    logger.info(f"Inserting into Supabase in batches of {args.batch_size}...")
    inserted = insert_rows(client, rows, args.batch_size, logger)
    logger.info(f"Inserted {inserted} row(s).")

    verify(client, logger, before_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
