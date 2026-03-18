#!/usr/bin/env python3
"""
CORDIS API Backfill — Phase 2 (API for uncovered projects)
============================================================
The bulk CORDIS enrichment (cordis_enrichment.py) covered 43,825 of 54,579
RESEARCH projects using H2020/HE organization Parquet files.  This script
backfills the remaining ~10,754 projects (mostly recent Horizon Europe
2024-2027) by querying the CORDIS search API one project at a time.

API endpoint:
    GET https://cordis.europa.eu/search?q='{project_id}'&type=project&format=json

Features:
    - Rate limiting (1 request/second, configurable)
    - JSON response caching (one file per project in cache/cordis_api/)
    - Full resume capability (skip already-cached projects)
    - Progress logging every 100 projects
    - Retry with exponential backoff (max 3 retries per request)
    - Graceful interruption handling

Output:
    - external_enrichment/output/cordis_api_participants.csv
        API-sourced participants in the same schema as cordis_participants.csv
    - external_enrichment/output/cordis_participants_combined.csv
        Merged bulk + API participants
    - external_enrichment/output/research_enriched.csv
        Regenerated enriched RESEARCH using combined participants

Dependencies: pandas, requests (standard + two common packages)

Usage:
    python -m src.enrichment.cordis_api_backfill
    python -m src.enrichment.cordis_api_backfill --dry-run
    python -m src.enrichment.cordis_api_backfill --rate-limit 0.5
"""

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
from src.paths import REPO_ROOT, PROCESSED_DIR, ENRICHMENT_DIR

OUTPUT_DIR = ENRICHMENT_DIR
CACHE_DIR = REPO_ROOT / "cache" / "cordis_api"

RESEARCH_CSV = PROCESSED_DIR / "standardized_RESEARCH.csv"
BULK_PARTICIPANTS_CSV = ENRICHMENT_DIR / "cordis_participants.csv"
API_PARTICIPANTS_CSV = ENRICHMENT_DIR / "cordis_api_participants.csv"
COMBINED_PARTICIPANTS_CSV = ENRICHMENT_DIR / "cordis_participants_combined.csv"
ENRICHED_CSV = ENRICHMENT_DIR / "research_enriched.csv"

# ---------------------------------------------------------------------------
# API config
# ---------------------------------------------------------------------------
CORDIS_SEARCH_URL = "https://cordis.europa.eu/search"
DEFAULT_RATE_LIMIT = 1.0  # seconds between requests
MAX_RETRIES = 3
INITIAL_BACKOFF = 2.0  # seconds, doubles on each retry
REQUEST_TIMEOUT = 30  # seconds

# Participants CSV schema (must match bulk output exactly)
PARTICIPANT_COLUMNS = [
    "project_id",
    "org_name",
    "org_short",
    "org_country",
    "activity_type",
    "role",
    "sme",
    "vat_number",
    "org_id",
    "ec_contribution",
    "net_ec_contribution",
    "total_cost",
    "cordis_programme",
]

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    log.warning("Shutdown signal received. Will finish current request and save progress...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------
def fetch_project(project_id: str, session: requests.Session) -> dict:
    """
    Query the CORDIS search API for a project ID.
    Returns the raw JSON response dict.
    Raises requests.RequestException on unrecoverable failure.
    """
    params = {
        "q": f"'{project_id}'",
        "type": "project",
        "format": "json",
    }

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                CORDIS_SEARCH_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                backoff = INITIAL_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    f"  Attempt {attempt}/{MAX_RETRIES} failed for project {project_id}: "
                    f"{exc}. Retrying in {backoff:.0f}s..."
                )
                time.sleep(backoff)
            else:
                log.error(
                    f"  All {MAX_RETRIES} attempts failed for project {project_id}: {last_exc}"
                )
                raise last_exc


def load_cached(project_id: str) -> dict | None:
    """Load cached API response for a project, or return None."""
    cache_file = CACHE_DIR / f"{project_id}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"  Corrupt cache for {project_id}, will re-fetch: {exc}")
            cache_file.unlink(missing_ok=True)
    return None


def save_cache(project_id: str, data: dict) -> None:
    """Save API response to cache."""
    cache_file = CACHE_DIR / f"{project_id}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def extract_activity_type(org: dict) -> str:
    """Extract activityType from the nested category structure."""
    cats = org.get("relations", {}).get("categories", {}).get("category", {})
    if isinstance(cats, dict):
        cats = [cats]
    for cat in cats:
        attrs = cat.get("@attributes", {})
        if attrs.get("classification") == "organizationActivityType":
            return cat.get("code", "")
    return ""


def parse_organizations(data: dict, target_project_id: str) -> list[dict]:
    """
    Parse API JSON response and extract organization rows for the target project.

    The CORDIS search API may return multiple hits (project + articles + results).
    We find the exact project matching our target ID and extract its organizations.
    """
    hits_container = data.get("hits", {})
    if not hits_container:
        return []

    hits = hits_container.get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]

    # Find the exact project hit matching our target ID
    target_project = None
    for hit in hits:
        project = hit.get("project")
        if project and str(project.get("id")) == str(target_project_id):
            target_project = project
            break

    if target_project is None:
        return []

    # Determine programme from the project's programme associations
    programme = _detect_programme(target_project)

    # Extract organizations
    orgs_raw = (
        target_project
        .get("relations", {})
        .get("associations", {})
        .get("organization", [])
    )
    if isinstance(orgs_raw, dict):
        orgs_raw = [orgs_raw]

    rows = []
    for org in orgs_raw:
        attrs = org.get("@attributes", {})
        address = org.get("address", {})

        # Determine role from @attributes.type (coordinator vs participant)
        role_raw = attrs.get("type", "participant")
        # Normalize: API uses "coordinator" / "participant", bulk uses same
        role = role_raw.lower() if role_raw else "participant"

        # SME: convert string "true"/"false" to Python bool
        sme_raw = attrs.get("sme", "false")
        sme = sme_raw.lower() == "true" if isinstance(sme_raw, str) else bool(sme_raw)

        rows.append({
            "project_id": int(target_project_id),
            "org_name": org.get("legalName", ""),
            "org_short": org.get("shortName", ""),
            "org_country": address.get("country", ""),
            "activity_type": extract_activity_type(org),
            "role": role,
            "sme": sme,
            "vat_number": org.get("vatNumber", ""),
            "org_id": float(org.get("id", 0)) if org.get("id") else None,
            "ec_contribution": _safe_float(attrs.get("ecContribution")),
            "net_ec_contribution": _safe_float(attrs.get("netEcContribution")),
            "total_cost": _safe_float(attrs.get("totalCost")),
            "cordis_programme": programme,
        })

    return rows


def _detect_programme(project: dict) -> str:
    """Detect framework programme from project associations."""
    programmes = (
        project
        .get("relations", {})
        .get("associations", {})
        .get("programme", [])
    )
    if isinstance(programmes, dict):
        programmes = [programmes]

    for prog in programmes:
        code = prog.get("code", "")
        if code.startswith("H2020"):
            return "H2020"
        if code.startswith("HORIZON"):
            return "HORIZON_EUROPE"

    # Fallback: check legal basis or other identifiers
    # Project IDs >= 101000000 are generally Horizon Europe
    try:
        pid = int(project.get("id", 0))
        if pid >= 101000000:
            return "HORIZON_EUROPE"
    except (ValueError, TypeError):
        pass

    return "H2020"


def _safe_float(val) -> float:
    """Convert a value to float, returning 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Identify projects needing backfill
# ---------------------------------------------------------------------------
def get_missing_project_ids() -> list[str]:
    """
    Compare RESEARCH source_record_ids against bulk participants to find
    projects that need API backfill.
    """
    log.info("Loading standardized RESEARCH...")
    research = pd.read_csv(RESEARCH_CSV)
    research_ids = set(
        research["source_record_id"]
        .dropna()
        .astype(str)
        .str.strip()
    )
    log.info(f"  Total RESEARCH projects: {len(research_ids):,}")

    log.info("Loading bulk participants...")
    if BULK_PARTICIPANTS_CSV.exists():
        bulk = pd.read_csv(BULK_PARTICIPANTS_CSV)
        bulk_ids = set(
            bulk["project_id"]
            .dropna()
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.strip()
        )
        log.info(f"  Bulk-covered projects: {len(bulk_ids):,}")
    else:
        log.warning(f"  Bulk participants not found at {BULK_PARTICIPANTS_CSV}")
        bulk_ids = set()

    missing = sorted(research_ids - bulk_ids)
    log.info(f"  Projects needing API backfill: {len(missing):,}")
    return missing


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------
def fetch_all(
    project_ids: list[str],
    rate_limit: float = DEFAULT_RATE_LIMIT,
    dry_run: bool = False,
) -> pd.DataFrame:
    """
    Fetch organization data for all project IDs via the CORDIS API.
    Uses cache for resume capability. Returns a DataFrame of all participants.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    total = len(project_ids)
    all_rows = []
    fetched_count = 0
    cached_count = 0
    empty_count = 0
    error_count = 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": "BruegelResearch/1.0 (EU subsidy analysis; academic use)",
        "Accept": "application/json",
    })

    log.info(f"Starting API backfill for {total:,} projects (rate: {rate_limit}s/req)")
    if dry_run:
        log.info("DRY RUN: will only check cache, no API calls")

    t0 = time.time()

    for i, pid in enumerate(project_ids):
        if _shutdown_requested:
            log.warning(f"Shutdown requested after {i:,} projects. Saving progress...")
            break

        # Check cache first
        cached_data = load_cached(pid)
        if cached_data is not None:
            rows = parse_organizations(cached_data, pid)
            all_rows.extend(rows)
            cached_count += 1
            if rows:
                fetched_count += 1
            else:
                empty_count += 1
            # No progress log for cache hits to avoid spam during resume
            if (i + 1) % 1000 == 0:
                elapsed = time.time() - t0
                log.info(
                    f"  [{i+1:,}/{total:,}] Cache replay... "
                    f"({cached_count:,} cached, {elapsed:.0f}s elapsed)"
                )
            continue

        if dry_run:
            continue

        # Rate limit
        if i > 0 and rate_limit > 0:
            time.sleep(rate_limit)

        # Fetch from API
        try:
            data = fetch_project(pid, session)
            save_cache(pid, data)

            rows = parse_organizations(data, pid)
            all_rows.extend(rows)

            if rows:
                fetched_count += 1
            else:
                empty_count += 1

        except requests.RequestException:
            error_count += 1
            # Save an empty sentinel to cache so we don't retry on resume
            # (the project genuinely failed or doesn't exist)
            save_cache(pid, {"_error": True, "hits": {}})

        # Progress logging
        api_calls = i + 1 - cached_count
        if api_calls > 0 and api_calls % 100 == 0:
            elapsed = time.time() - t0
            rate = api_calls / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate if rate > 0 else 0
            log.info(
                f"  [{i+1:,}/{total:,}] "
                f"API calls: {api_calls:,} | "
                f"With orgs: {fetched_count:,} | "
                f"Empty: {empty_count:,} | "
                f"Errors: {error_count:,} | "
                f"Rate: {rate:.2f} req/s | "
                f"ETA: {remaining/60:.0f}min"
            )

    elapsed = time.time() - t0
    log.info(f"Fetch complete in {elapsed:.0f}s")
    log.info(f"  Total projects processed: {fetched_count + empty_count + error_count:,}")
    log.info(f"  Projects with organizations: {fetched_count:,}")
    log.info(f"  Projects with no orgs (empty): {empty_count:,}")
    log.info(f"  Projects with errors: {error_count:,}")
    log.info(f"  From cache: {cached_count:,}")
    log.info(f"  Total org rows: {len(all_rows):,}")

    if not all_rows:
        return pd.DataFrame(columns=PARTICIPANT_COLUMNS)

    df = pd.DataFrame(all_rows, columns=PARTICIPANT_COLUMNS)
    return df


# ---------------------------------------------------------------------------
# Merge & re-enrich
# ---------------------------------------------------------------------------
def merge_participants(api_participants: pd.DataFrame) -> pd.DataFrame:
    """Merge bulk + API participants into a combined file."""
    frames = []

    if BULK_PARTICIPANTS_CSV.exists():
        bulk = pd.read_csv(BULK_PARTICIPANTS_CSV)
        log.info(f"  Bulk participants: {len(bulk):,} rows, {bulk['project_id'].nunique():,} projects")
        frames.append(bulk)

    if len(api_participants) > 0:
        log.info(
            f"  API participants: {len(api_participants):,} rows, "
            f"{api_participants['project_id'].nunique():,} projects"
        )
        frames.append(api_participants)

    if not frames:
        raise RuntimeError("No participant data to merge")

    combined = pd.concat(frames, ignore_index=True)

    # Deduplicate: same project_id + org_id should not appear twice
    before = len(combined)
    combined = combined.drop_duplicates(subset=["project_id", "org_id", "org_name"], keep="first")
    after = len(combined)
    if before != after:
        log.info(f"  Deduplication removed {before - after:,} rows")

    log.info(
        f"  Combined participants: {len(combined):,} rows, "
        f"{combined['project_id'].nunique():,} projects"
    )
    return combined


def regenerate_enriched(combined_participants: pd.DataFrame) -> None:
    """
    Regenerate research_enriched.csv using the combined participants.
    Reuses the enrichment logic from cordis_enrichment.py.
    """
    log.info("Regenerating enriched RESEARCH dataset...")

    # Import the enrichment function from the sibling module
    from src.enrichment.cordis_enrichment import build_enriched_research

    # Load RESEARCH
    research = pd.read_csv(RESEARCH_CSV)
    log.info(f"  RESEARCH: {len(research):,} rows")

    # Convert combined participants back to the 'orgs' format expected by
    # build_enriched_research (which uses the original column names)
    orgs = combined_participants.rename(columns={
        "project_id": "projectID",
        "org_name": "name",
        "org_short": "shortName",
        "org_country": "country",
        "activity_type": "activityType",
        "role": "role",
        "sme": "SME",
        "vat_number": "vatNumber",
        "org_id": "organisationID",
        "ec_contribution": "ecContribution",
        "net_ec_contribution": "netEcContribution",
        "total_cost": "totalCost",
    })

    # Ensure projectID is Int64 as expected
    orgs["projectID"] = pd.to_numeric(orgs["projectID"], errors="coerce").astype("Int64")

    # Build enriched dataset
    enriched = build_enriched_research(research, orgs)
    enriched.to_csv(ENRICHED_CSV, index=False)
    log.info(f"  Saved: {ENRICHED_CSV} ({len(enriched):,} rows)")

    # Summary
    beneficiary_rows = enriched[enriched["resolution_level"] == "beneficiary"]
    project_rows = enriched[enriched["resolution_level"] == "project"]
    log.info(f"  Beneficiary-level rows: {len(beneficiary_rows):,}")
    log.info(f"  Project-level (unmatched): {len(project_rows):,}")
    log.info(
        f"  Beneficiary EUR: {beneficiary_rows['amount_eur'].sum():,.0f}"
    )
    log.info(
        f"  Unmatched EUR: {project_rows['amount_eur'].sum():,.0f}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="CORDIS API backfill for uncovered RESEARCH projects"
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=DEFAULT_RATE_LIMIT,
        help=f"Seconds between API requests (default: {DEFAULT_RATE_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only process cached data, do not make API calls",
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="Skip the final enrichment step (just fetch and save participants)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of projects to fetch (0 = all, useful for testing)",
    )
    args = parser.parse_args()

    t0 = time.time()

    log.info("=" * 70)
    log.info("CORDIS API BACKFILL — Phase 2")
    log.info("=" * 70)

    # Step 1: Identify missing projects
    missing_ids = get_missing_project_ids()

    if not missing_ids:
        log.info("No projects need API backfill. All covered by bulk data.")
        return

    if args.limit > 0:
        missing_ids = missing_ids[: args.limit]
        log.info(f"  Limited to first {args.limit:,} projects (--limit)")

    # Step 2: Fetch from API (with caching)
    log.info("")
    log.info("FETCHING FROM CORDIS API")
    log.info("-" * 40)
    api_participants = fetch_all(
        missing_ids,
        rate_limit=args.rate_limit,
        dry_run=args.dry_run,
    )

    # Step 3: Save API participants
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    api_participants.to_csv(API_PARTICIPANTS_CSV, index=False)
    log.info(f"Saved API participants: {API_PARTICIPANTS_CSV} ({len(api_participants):,} rows)")

    # Step 4: Merge bulk + API
    log.info("")
    log.info("MERGING PARTICIPANTS")
    log.info("-" * 40)
    combined = merge_participants(api_participants)
    combined.to_csv(COMBINED_PARTICIPANTS_CSV, index=False)
    log.info(f"Saved combined participants: {COMBINED_PARTICIPANTS_CSV} ({len(combined):,} rows)")

    # Step 5: Regenerate enriched RESEARCH
    if not args.skip_enrich:
        log.info("")
        log.info("REGENERATING ENRICHED RESEARCH")
        log.info("-" * 40)
        try:
            regenerate_enriched(combined)
        except Exception as exc:
            log.error(f"Enrichment failed: {exc}")
            log.error("API participants and combined files are still saved.")
            log.error("You can re-run with --skip-enrich and do enrichment separately.")
            raise

    elapsed = time.time() - t0
    log.info("")
    log.info("=" * 70)
    log.info(f"BACKFILL COMPLETE in {elapsed:.0f}s ({elapsed/60:.1f}min)")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
