#!/usr/bin/env python3
"""
AI Job Agent - Part 1: Job Finder
=================================

What this program does, in order:

  1. Waits until one of the times listed in config.json -> "run_times"
     (or runs immediately when started with --once).
  2. Downloads fresh job adverts from Reed (UK), Adzuna (many countries,
     including Poland) and the free solid.jobs and Himalayas APIs
     (Polish IT board and remote roles).
  3. Removes duplicates and adverts it has already processed before.
  4. Throws away adverts that clearly do not fit (wrong title, too senior,
     or containing a "skip" phrase such as a security-clearance requirement).
  5. Asks an AI model to score every remaining advert against YOUR master
     profile (cv/master.md), from 0 to 100. Two providers:
       - Ollama (default): a local model on your own server, free and private
       - Mistral:   set AI_PROVIDER=mistral and MISTRAL_API_KEY in .env to use
                    the Mistral API instead (no GPU needed, runs anywhere)
  6. Saves every advert that scores at or above "min_match_score" into the
     NocoDB tracker table with Status = "Found".
  7. Sends you a Telegram summary of the best matches.

It never applies for jobs and never changes your CV. Those are planned
future parts (see the handbook roadmap).

Run modes:
  python finder.py           -> scheduler mode (stays running, runs at run_times)
  python finder.py --once    -> one full run now, then exit

All secrets come from environment variables (the .env file), never from
this file.
"""

import datetime as dt
import html
import json
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Paths (inside the container, /data is the host folder /opt/jobagent)
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "app", "config.json")
MASTER_FILE = os.path.join(DATA_DIR, "cv", "master.md")
SEEN_FILE = os.path.join(DATA_DIR, "state", "seen.json")

# ---------------------------------------------------------------------------
# Settings from the environment (.env)
# ---------------------------------------------------------------------------
REED_API_KEY = os.environ.get("REED_API_KEY", "").strip()
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "").strip()
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NOCODB_URL = os.environ.get("NOCODB_URL", "http://nocodb:8080").rstrip("/")
NOCODB_TOKEN = os.environ.get("NOCODB_TOKEN", "").strip()
NOCODB_TABLE_ID = os.environ.get("NOCODB_TABLE_ID", "").strip()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
SCORING_MODEL = os.environ.get("SCORING_MODEL", "qwen2.5:3b").strip()

# --- AI provider: "ollama" (local, free) or "mistral" (API, no GPU needed) ---
AI_PROVIDER = (os.environ.get("AI_PROVIDER", "ollama").strip().lower() or "ollama")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "").strip()
MISTRAL_BASE = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai").rstrip("/")

# Base URLs can be overridden for testing; normal users never change these.
REED_BASE = os.environ.get("REED_BASE_URL", "https://www.reed.co.uk/api/1.0").rstrip("/")
ADZUNA_BASE = os.environ.get("ADZUNA_BASE_URL", "https://api.adzuna.com/v1/api").rstrip("/")
SOLID_BASE = os.environ.get("SOLID_BASE_URL", "https://solid.jobs").rstrip("/")
HIMALAYAS_BASE = os.environ.get("HIMALAYAS_BASE_URL", "https://himalayas.app").rstrip("/")
TELEGRAM_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# AI scoring limits (explained in the handbook, chapter "AI configuration")
AI_TIMEOUT_SECONDS = 360      # give up on one advert after 6 minutes (Ollama)
MISTRAL_TIMEOUT_SECONDS = 60  # the Mistral API normally answers in a few seconds
AI_NUM_CTX = 8192             # context window: master profile + advert must fit
AI_NUM_PREDICT = 300          # max answer length; stops endless-whitespace bug
DESCRIPTION_LIMIT = 2500      # characters of the advert sent to the AI

HTTP_TIMEOUT = 30
PLACEHOLDER_MARK = "YOUR_"


def log(message):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def is_set(value):
    return bool(value) and PLACEHOLDER_MARK not in value


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("run_times", ["07:30", "18:30"])
    cfg.setdefault("search_keywords", [])
    cfg.setdefault("search_countries", [])
    cfg.setdefault("solidjobs_enabled", True)
    cfg.setdefault("solidjobs_campaign", "ai-job-agent")
    cfg.setdefault("himalayas_enabled", True)
    cfg.setdefault("location", "")
    cfg.setdefault("distance_miles", 20)
    cfg.setdefault("title_must_contain_one_of", [])
    cfg.setdefault("avoid_title_keywords", [])
    cfg.setdefault("skip_if_description_contains", [])
    cfg.setdefault("max_days_old", 3)
    cfg.setdefault("max_jobs_to_score_per_run", 40)
    cfg.setdefault("min_match_score", 70)
    cfg.setdefault("telegram_top_n", 15)
    return cfg


def load_master():
    with open(MASTER_FILE, encoding="utf-8") as f:
        return f.read()


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, TypeError):
        log("WARNING: seen.json was unreadable, starting with an empty list")
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)
    os.replace(tmp, SEEN_FILE)  # atomic: never leaves a half-written file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_html(text):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def contains_word(haystack, word):
    """Whole-word, case-insensitive match ('Sr' must not match 'Srinagar')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])",
                     haystack.lower()) is not None


def parse_reed_date(value):
    try:
        return dt.datetime.strptime(value, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def parse_iso_date(value):
    """Parse ISO dates like '2026-11-12T16:55:10.78+01:00' into a date."""
    try:
        return dt.datetime.fromisoformat((value or "")[:19]).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Job sources
# ---------------------------------------------------------------------------
def search_countries(cfg):
    """Countries to search, e.g. [{"code": "gb", "location": "London", "distance_miles": 20}].

    Codes are Adzuna country codes ("gb", "pl", ...). Reed is UK-only and only
    runs when "gb" is in the list. Older config files with a single top-level
    "location" keep working: they are treated as the UK entry.
    """
    countries = cfg.get("search_countries") or []
    if countries:
        return countries
    return [{"code": "gb", "location": cfg.get("location", ""),
             "distance_miles": cfg.get("distance_miles", 20)}]


def fetch_reed(cfg):
    # Reed only lists jobs posted on reed.co.uk (UK only).
    uk = [c for c in search_countries(cfg) if (c.get("code") or "gb").lower() == "gb"]
    if not uk:
        log("Reed: UK not in search_countries, skipping Reed")
        return []
    if not is_set(REED_API_KEY):
        log("Reed: no REED_API_KEY in .env, skipping Reed")
        return []
    country = uk[0]
    jobs = []
    oldest = dt.date.today() - dt.timedelta(days=int(cfg["max_days_old"]))
    for keyword in cfg["search_keywords"]:
        params = {"keywords": keyword, "fullTime": "true", "resultsToTake": 100}
        if country.get("location"):
            params["locationName"] = country["location"]
            params["distanceFromLocation"] = country.get("distance_miles", 20)
        try:
            r = requests.get(f"{REED_BASE}/search", params=params,
                             auth=(REED_API_KEY, ""), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as exc:  # one bad keyword must not stop the run
            log(f"Reed: '{keyword}' failed: {exc}")
            continue
        for item in results:
            posted = parse_reed_date(item.get("date"))
            if posted and posted < oldest:
                continue
            jobs.append({
                "id": f"reed-{item.get('jobId')}",
                "source": "Reed",
                "source_job_id": item.get("jobId"),
                "country": "GB",
                "title": item.get("jobTitle") or "",
                "company": item.get("employerName") or "",
                "location": item.get("locationName") or "",
                "url": item.get("jobUrl") or "",
                "description": strip_html(item.get("jobDescription") or ""),
            })
        log(f"Reed: '{keyword}' -> {len(results)} results")
    return jobs


def fetch_adzuna(cfg):
    if not (is_set(ADZUNA_APP_ID) and is_set(ADZUNA_APP_KEY)):
        log("Adzuna: ADZUNA_APP_ID or ADZUNA_APP_KEY missing in .env, skipping Adzuna")
        return []
    jobs = []
    for country in search_countries(cfg):
        code = (country.get("code") or "gb").lower()
        for keyword in cfg["search_keywords"]:
            params = {
                "app_id": ADZUNA_APP_ID,
                "app_key": ADZUNA_APP_KEY,
                "what_phrase": keyword,
                "full_time": 1,
                "max_days_old": int(cfg["max_days_old"]),
                "results_per_page": 50,
                "content-type": "application/json",
            }
            if country.get("location"):
                params["where"] = country["location"]
                # Adzuna expects kilometres, the config stores miles
                params["distance"] = int(round(float(country.get("distance_miles", 20)) * 1.609))
            try:
                r = requests.get(f"{ADZUNA_BASE}/jobs/{code}/search/1", params=params,
                                 timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception as exc:
                log(f"Adzuna [{code.upper()}]: '{keyword}' failed: {exc}")
                continue
            for item in results:
                jobs.append({
                    "id": f"adzuna-{item.get('id')}",
                    "source": "Adzuna",
                    "source_job_id": item.get("id"),
                    "country": code.upper(),
                    "title": strip_html(item.get("title") or ""),
                    "company": (item.get("company") or {}).get("display_name", ""),
                    "location": (item.get("location") or {}).get("display_name", ""),
                    "url": item.get("redirect_url") or "",
                    "description": strip_html(item.get("description") or ""),
                })
            log(f"Adzuna [{code.upper()}]: '{keyword}' -> {len(results)} results")
    return jobs


def fetch_reed_details(job):
    """Reed search results only contain a short snippet; fetch the full advert."""
    try:
        r = requests.get(f"{REED_BASE}/jobs/{job['source_job_id']}",
                         auth=(REED_API_KEY, ""), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        full = strip_html(r.json().get("jobDescription") or "")
        if full:
            job["description"] = full
    except Exception as exc:
        log(f"Reed details failed for {job['id']}: {exc} (using snippet)")
    return job


def fetch_solidjobs(cfg):
    """solid.jobs - Polish IT board with a free, keyless public API.

    The 'campaign' parameter is not a secret: the API asks integrators to
    send a name identifying their service, purely for traffic statistics.
    """
    countries = search_countries(cfg)
    if not any((c.get("code") or "gb").lower() == "pl" for c in countries):
        log("solid.jobs: PL not in search_countries, skipping solid.jobs")
        return []
    if not cfg.get("solidjobs_enabled", True):
        log("solid.jobs: disabled (solidjobs_enabled = false), skipping")
        return []
    campaign = re.sub(r"[^a-z0-9-]+", "-",
                      str(cfg.get("solidjobs_campaign") or "ai-job-agent")
                      .lower()).strip("-")[:64]
    pl = [c for c in countries if (c.get("code") or "gb").lower() == "pl"][0]
    oldest = dt.date.today() - dt.timedelta(days=int(cfg["max_days_old"]))
    jobs = []
    for keyword in cfg["search_keywords"]:
        params = {"campaign": campaign, "search.searchTerm": keyword,
                  "pageIndex": 0, "pageSize": 100}
        if pl.get("location"):
            params["search.cities"] = pl["location"]
        try:
            r = requests.get(f"{SOLID_BASE}/public-api/offers/IT", params=params,
                             headers={"X-Api-Version": "1.0"}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            results = r.json().get("jobs", [])
        except Exception as exc:
            log(f"solid.jobs: '{keyword}' failed: {exc}")
            continue
        kept = 0
        for item in results:
            posted = parse_iso_date(item.get("validFrom"))
            if posted and posted < oldest:
                continue
            location = ", ".join(item.get("locations") or [])
            if item.get("isRemote"):
                location = f"{location}, remote" if location else "Remote"
            jobs.append({
                "id": f"solid-{item.get('jobOfferKey')}",
                "source": "solid.jobs",
                "source_job_id": item.get("jobOfferKey"),
                "country": "PL",
                "title": item.get("title") or "",
                "company": item.get("company") or "",
                "location": location,
                "url": item.get("url") or "",
                "description": strip_html(item.get("description") or ""),
            })
            kept += 1
        log(f"solid.jobs: '{keyword}' -> {len(results)} results, {kept} fresh")
    return jobs


def fetch_himalayas(cfg):
    """Himalayas - free public API for remote jobs, filterable by country.

    Their data refreshes every 24 hours, so one call per run is enough.
    """
    if not cfg.get("himalayas_enabled", True):
        log("Himalayas: disabled (himalayas_enabled = false), skipping")
        return []
    oldest = dt.date.today() - dt.timedelta(days=int(cfg["max_days_old"]))
    jobs = []
    for keyword in cfg["search_keywords"]:
        for country in search_countries(cfg):
            code = (country.get("code") or "gb").upper()
            params = {"q": keyword, "country": code,
                      "sort": "recent", "limit": 20}
            try:
                r = requests.get(f"{HIMALAYAS_BASE}/jobs/api/search", params=params,
                                 timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                results = r.json().get("jobs", [])
            except Exception as exc:
                log(f"Himalayas [{code}]: '{keyword}' failed: {exc}")
                continue
            kept = 0
            for item in results:
                posted = None
                if item.get("pubDate"):
                    try:
                        posted = dt.date.fromtimestamp(int(item["pubDate"]))
                    except (ValueError, OSError):
                        posted = None
                if posted and posted < oldest:
                    continue
                restrictions = item.get("locationRestrictions") or []
                location = (f"Remote, {', '.join(restrictions)}"
                            if restrictions else "Remote (worldwide)")
                jobs.append({
                    "id": f"himalayas-{item.get('guid')}",
                    "source": "Himalayas",
                    "source_job_id": item.get("guid"),
                    "country": code,
                    "title": item.get("title") or "",
                    "company": item.get("companyName") or "",
                    "location": location,
                    "url": item.get("applicationLink") or "",
                    "description": strip_html(item.get("description") or ""),
                })
                kept += 1
            log(f"Himalayas [{code}]: '{keyword}' -> {len(results)} results, {kept} fresh")
    return jobs


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def dedupe(jobs):
    out, ids, pairs = [], set(), set()
    for job in jobs:
        pair = (norm(job["title"]), norm(job["company"]))
        if job["id"] in ids or pair in pairs:
            continue
        ids.add(job["id"])
        pairs.add(pair)
        out.append(job)
    return out


def rule_check(job, cfg):
    """Returns None if the job passes, otherwise a short reason string."""
    title = job["title"]
    wanted = cfg["title_must_contain_one_of"]
    if wanted and not any(w.lower() in title.lower() for w in wanted):
        return "title not relevant"
    for word in cfg["avoid_title_keywords"]:
        if contains_word(title, word):
            return f"seniority word '{word}'"
    desc = job["description"].lower()
    for phrase in cfg["skip_if_description_contains"]:
        if phrase.lower() in desc:
            return f"skip phrase '{phrase}'"
    return None


# ---------------------------------------------------------------------------
# AI scoring (Ollama or Mistral API - see AI_PROVIDER)
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTIONS = """You are an experienced recruiter screening job adverts for ONE candidate.
The candidate's complete, truthful profile is below. Judge only from facts in the profile.
Never assume skills, experience or qualifications that the profile does not state.

Score how well the candidate fits the advert from 0 to 100:
  90-100 excellent fit, apply today
  70-89  good fit, worth applying
  50-69  partial fit, important gaps
  0-49   poor fit

Reply with JSON only, exactly in this shape:
{"score": <integer 0-100>,
 "level_fit": "<too junior | good | too senior>",
 "reasons": ["<short reason>", "<short reason>"],
 "gaps": ["<short gap>"],
 "sponsorship_red_flag": <true if the advert says it cannot sponsor visas or requires
                          citizenship/security clearance the profile does not have, else false>}

=== CANDIDATE PROFILE ===
"""


def build_messages(job, master):
    """The profile goes FIRST and never changes between adverts, so Ollama can
    reuse it (prompt-prefix caching) and Mistral's API behaves predictably."""
    system_prompt = SYSTEM_INSTRUCTIONS + master
    advert = (
        f"JOB TITLE: {job['title']}\n"
        f"COMPANY: {job['company']}\n"
        f"COUNTRY: {job.get('country', '')}\n"
        f"LOCATION: {job['location']}\n"
        f"DESCRIPTION:\n{job['description'][:DESCRIPTION_LIMIT]}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": advert},
    ]


def score_with_ollama(messages):
    payload = {
        "model": SCORING_MODEL,
        "stream": False,
        "format": "json",
        "messages": messages,
        "options": {"num_ctx": AI_NUM_CTX, "temperature": 0.1,
                    "num_predict": AI_NUM_PREDICT},
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=AI_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "")


def score_with_mistral(messages):
    if not is_set(MISTRAL_API_KEY):
        raise RuntimeError("AI_PROVIDER=mistral but MISTRAL_API_KEY is missing in .env")
    payload = {
        "model": SCORING_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": AI_NUM_PREDICT,
        "response_format": {"type": "json_object"},  # Mistral JSON mode
    }
    r = requests.post(
        f"{MISTRAL_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}",
                 "Content-Type": "application/json"},
        json=payload, timeout=MISTRAL_TIMEOUT_SECONDS)
    if r.status_code == 401:
        raise RuntimeError("Mistral API key rejected (401): check MISTRAL_API_KEY")
    if r.status_code == 429:
        raise RuntimeError("Mistral rate limit hit (429): job will be retried next run")
    r.raise_for_status()
    choices = r.json().get("choices", [])
    if not choices:
        raise RuntimeError("Mistral returned no choices")
    return choices[0].get("message", {}).get("content", "")


def parse_score(content):
    """Both providers return the same JSON answer; parse it the same way."""
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError(f"AI did not return JSON: {content[:120]!r}")
    result = json.loads(match.group(0))
    score = int(float(result.get("score", 0)))
    result["score"] = max(0, min(100, score))
    result["reasons"] = [str(x) for x in (result.get("reasons") or [])][:5]
    result["gaps"] = [str(x) for x in (result.get("gaps") or [])][:5]
    flag = result.get("sponsorship_red_flag", False)
    result["sponsorship_red_flag"] = flag is True or str(flag).lower() == "true"
    result["level_fit"] = str(result.get("level_fit", ""))
    return result


def score_job(job, master):
    if AI_PROVIDER not in ("ollama", "mistral"):
        raise RuntimeError(
            f"AI_PROVIDER={AI_PROVIDER!r} is unknown: use 'ollama' or 'mistral'")
    messages = build_messages(job, master)
    if AI_PROVIDER == "mistral":
        content = score_with_mistral(messages)
    else:
        content = score_with_ollama(messages)
    return parse_score(content)


# ---------------------------------------------------------------------------
# Outputs: NocoDB tracker and Telegram
# ---------------------------------------------------------------------------
def save_to_nocodb(job, result):
    if not (is_set(NOCODB_TOKEN) and is_set(NOCODB_TABLE_ID)):
        log("NocoDB: token or table ID missing in .env, not saving")
        return False
    notes = (
        f"Level fit: {result['level_fit']}\n"
        f"Reasons: {'; '.join(result['reasons'])}\n"
        f"Gaps: {'; '.join(result['gaps'])}\n"
        f"Country: {job.get('country', '')}\n"
        f"Location: {job['location']}"
    )
    record = {
        "Company": job["company"],
        "Role": job["title"],
        "Link": job["url"],
        "Source": job["source"],
        "Match Score": result["score"],
        "Status": "Found",
        "Notes": notes,
        "Job ID": job["id"],
    }
    try:
        r = requests.post(
            f"{NOCODB_URL}/api/v2/tables/{NOCODB_TABLE_ID}/records",
            headers={"xc-token": NOCODB_TOKEN}, json=record, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as exc:
        log(f"NocoDB: could not save {job['id']}: {exc}")
        return False


def send_telegram(text):
    if not (is_set(TELEGRAM_TOKEN) and is_set(TELEGRAM_CHAT_ID)):
        log("Telegram: token or chat ID missing in .env, message not sent")
        return False
    ok = True
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        try:
            r = requests.post(
                f"{TELEGRAM_BASE}/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as exc:
            # never print the token: the URL contains it
            log(f"Telegram: send failed ({type(exc).__name__})")
            ok = False
    return ok


def telegram_summary(matches, stats, cfg):
    e = html.escape
    if not matches:
        return (f"<b>Job Finder</b>: run finished.\n"
                f"Fetched {stats['fetched']}, passed filters {stats['passed']}, "
                f"scored {stats['scored']}.\n"
                f"No new jobs above {cfg['min_match_score']} this time.")
    top = sorted(matches, key=lambda m: m[1]["score"], reverse=True)[:cfg["telegram_top_n"]]
    lines = [f"<b>Job Finder</b>: {len(matches)} new match(es) "
             f"(score {cfg['min_match_score']}+)",
             f"Fetched {stats['fetched']} | passed filters {stats['passed']} | "
             f"scored {stats['scored']}", ""]
    for job, res in top:
        reason = res["reasons"][0] if res["reasons"] else ""
        where = job["location"]
        if job.get("country") and job["country"] not in where.upper():
            where = f"{where}, {job['country']}" if where else job["country"]
        lines.append(f"<b>{res['score']}</b> - {e(job['title'])} @ {e(job['company'])}")
        if where:
            lines.append(f"   {e(where)} ({e(job['source'])})")
        if reason:
            lines.append(f"   {e(reason)}")
        if job["url"]:
            lines.append(f"   <a href=\"{e(job['url'], quote=True)}\">Open advert</a>")
        lines.append("")
    lines.append("All matches are saved in your NocoDB tracker with Status = Found.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# One full run
# ---------------------------------------------------------------------------
def run_once():
    cfg = load_config()
    master = load_master()
    seen = load_seen()
    log(f"Run started (AI_PROVIDER {AI_PROVIDER}, model {SCORING_MODEL}, "
        f"threshold {cfg['min_match_score']})")

    jobs = dedupe(fetch_reed(cfg) + fetch_adzuna(cfg)
                  + fetch_solidjobs(cfg) + fetch_himalayas(cfg))
    fetched = len(jobs)
    jobs = [j for j in jobs if j["id"] not in seen]
    log(f"Fetched {fetched} unique jobs, {len(jobs)} not seen before")

    candidates = []
    for job in jobs:
        reason = rule_check(job, cfg)
        if reason:
            seen.add(job["id"])  # rejected by rules: never look at it again
        else:
            candidates.append(job)
    save_seen(seen)
    log(f"{len(candidates)} jobs passed the filters")

    limit = int(cfg["max_jobs_to_score_per_run"])
    if len(candidates) > limit:
        log(f"Scoring the first {limit}; the other {len(candidates) - limit} "
            f"stay unseen and carry over to the next run")
    to_score = candidates[:limit]

    matches, scored = [], 0
    for n, job in enumerate(to_score, 1):
        if job["source"] == "Reed":
            fetch_reed_details(job)
            # the full advert may contain a skip phrase the snippet did not
            reason = rule_check(job, cfg)
            if reason:
                log(f"[{n}/{len(to_score)}] skipped after full text: {reason}")
                seen.add(job["id"])
                save_seen(seen)
                continue
        started = time.time()
        try:
            result = score_job(job, master)
        except Exception as exc:
            # NOT marked as seen, so it will be tried again next run
            log(f"[{n}/{len(to_score)}] scoring failed for {job['title']!r}: {exc}")
            continue
        scored += 1
        seen.add(job["id"])  # mark seen only AFTER a successful score
        save_seen(seen)
        took = time.time() - started
        log(f"[{n}/{len(to_score)}] {result['score']:>3} | {job['title']} @ "
            f"{job['company']} ({took:.0f}s)")
        if result["sponsorship_red_flag"]:
            log("      dropped: sponsorship/clearance red flag")
            continue
        if result["score"] >= int(cfg["min_match_score"]):
            save_to_nocodb(job, result)
            matches.append((job, result))

    stats = {"fetched": fetched, "passed": len(candidates), "scored": scored}
    send_telegram(telegram_summary(matches, stats, cfg))
    log(f"Run finished: {scored} scored, {len(matches)} saved as matches")
    return matches


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def scheduler():
    log("Scheduler started. Checking the clock every 20 seconds.")
    done = set()  # (date, "HH:MM") pairs already run
    while True:
        try:
            cfg = load_config()
            now = dt.datetime.now()
            slot = now.strftime("%H:%M")
            key = (now.date().isoformat(), slot)
            if slot in cfg["run_times"] and key not in done:
                done.add(key)
                run_once()
        except Exception as exc:  # keep the scheduler alive no matter what
            log(f"ERROR during run: {exc}")
        time.sleep(20)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        scheduler()
