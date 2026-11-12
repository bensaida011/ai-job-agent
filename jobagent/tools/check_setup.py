#!/usr/bin/env python3
"""
Helper: test every connection the Job Finder needs, one by one.

Run (on the server):
  docker exec jobagent python /data/tools/check_setup.py

Each line ends in OK, SKIP or FAIL with a hint. Add --send to also send a
test message to your Telegram. Secrets are never printed.
"""
import json
import os
import sys

import requests

E = os.environ.get
DATA = E("DATA_DIR", "/data")
REED_BASE = E("REED_BASE_URL", "https://www.reed.co.uk/api/1.0").rstrip("/")
ADZUNA_BASE = E("ADZUNA_BASE_URL", "https://api.adzuna.com/v1/api").rstrip("/")
SOLID_BASE = E("SOLID_BASE_URL", "https://solid.jobs").rstrip("/")
HIMALAYAS_BASE = E("HIMALAYAS_BASE_URL", "https://himalayas.app").rstrip("/")
JOOBLE_BASE = E("JOOBLE_BASE_URL", "https://jooble.org").rstrip("/")
APIFY_BASE = E("APIFY_BASE_URL", "https://api.apify.com/v2").rstrip("/")
APIFY_ACTOR = E("JUSTJOIN_ACTOR_ID", "unfenced-group~justjoin-scraper")
TG_BASE = E("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
failures = 0


def is_set(name):
    v = (E(name) or "").strip()
    return bool(v) and "YOUR_" not in v


def report(name, status, hint=""):
    global failures
    if status == "FAIL":
        failures += 1
    print(f"  {name:<22} {status:<5} {hint}")


def check(name, func):
    try:
        status, hint = func()
    except requests.RequestException as exc:
        status, hint = "FAIL", f"cannot connect ({type(exc).__name__})"
    except Exception as exc:
        status, hint = "FAIL", str(exc)[:120]
    report(name, status, hint)


def files():
    for rel in ("app/config.json", "cv/master.md"):
        if not os.path.isfile(os.path.join(DATA, rel)):
            return "FAIL", f"missing {rel} in /opt/jobagent"
    with open(os.path.join(DATA, "app/config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(DATA, "cv/master.md"), encoding="utf-8") as f:
        words = len(f.read().split())
    hint = f"{len(cfg.get('search_keywords', []))} keywords, master.md {words} words"
    return "OK", hint


def reed():
    if not is_set("REED_API_KEY"):
        return "FAIL", "REED_API_KEY not set in .env"
    r = requests.get(f"{REED_BASE}/search", params={"keywords": "analyst", "resultsToTake": 1},
                     auth=(E("REED_API_KEY").strip(), ""), timeout=30)
    if r.status_code == 401:
        return "FAIL", "key rejected (401): copy it again from the Reed email"
    r.raise_for_status()
    return "OK", f"{r.json().get('totalResults', '?')} jobs available"


def adzuna():
    if not (is_set("ADZUNA_APP_ID") and is_set("ADZUNA_APP_KEY")):
        return "SKIP", "not configured (optional)"
    countries = ["gb"]
    try:
        with open(os.path.join(DATA, "app/config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        countries = [c.get("code", "gb") for c in (cfg.get("search_countries") or [{"code": "gb"}])]
    except (OSError, json.JSONDecodeError):
        pass
    counts = []
    for code in countries:
        r = requests.get(f"{ADZUNA_BASE}/jobs/{code}/search/1",
                         params={"app_id": E("ADZUNA_APP_ID").strip(),
                                 "app_key": E("ADZUNA_APP_KEY").strip(),
                                 "what": "analyst", "results_per_page": 1}, timeout=30)
        if r.status_code in (401, 403):
            return "FAIL", "rejected: APP_ID must be the short 8-character id"
        r.raise_for_status()
        counts.append(f"{code}: {r.json().get('count', '?')} jobs")
    return "OK", ", ".join(counts)


def ollama():
    url = E("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    model = E("SCORING_MODEL", "qwen2.5:3b")
    r = requests.get(f"{url}/api/tags", timeout=15)
    r.raise_for_status()
    names = [m.get("name", "") for m in r.json().get("models", [])]
    if model not in names and f"{model}:latest" not in names:
        return "FAIL", f"model {model} not downloaded: docker exec ollama ollama pull {model}"
    return "OK", f"{model} is installed"


def mistral():
    if not is_set("MISTRAL_API_KEY"):
        return "FAIL", "AI_PROVIDER=mistral but MISTRAL_API_KEY not set in .env"
    base = E("MISTRAL_BASE_URL", "https://api.mistral.ai").rstrip("/")
    model = E("SCORING_MODEL", "mistral-small-latest")
    headers = {"Authorization": f"Bearer {E('MISTRAL_API_KEY').strip()}"}
    r = requests.get(f"{base}/v1/models", headers=headers, timeout=30)
    if r.status_code == 401:
        return "FAIL", "API key rejected (401): create a new key at console.mistral.ai"
    r.raise_for_status()
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    if ids and model not in ids:
        return "FAIL", f"model {model} not available; valid ids include: {', '.join(ids[:5])}"
    # cheap real call: proves key + model + JSON mode all work
    s = requests.post(f"{base}/v1/chat/completions", headers=headers,
                      json={"model": model, "messages": [{"role": "user", "content": "Reply with {\"ok\": true}"}],
                            "max_tokens": 20, "response_format": {"type": "json_object"}},
                      timeout=60)
    if s.status_code in (401, 403):
        return "FAIL", "rejected: check MISTRAL_API_KEY and your Mistral plan"
    if s.status_code == 422:
        return "FAIL", f"model {model} refused the request: check SCORING_MODEL spelling"
    s.raise_for_status()
    return "OK", f"chat works, model {model}"


def ai():
    provider = (E("AI_PROVIDER") or "ollama").strip().lower()
    if provider == "mistral":
        return mistral()
    if provider == "ollama":
        return ollama()
    return "FAIL", f"AI_PROVIDER={provider!r} unknown: use ollama or mistral"


def solidjobs(base):
    """Free keyless Polish IT board; campaign just names our service."""
    r = requests.get(f"{base}/public-api/offers/IT",
                     params={"campaign": "ai-job-agent-setup-check", "pageSize": 1},
                     headers={"X-Api-Version": "1.0"}, timeout=30)
    r.raise_for_status()
    return "OK", f"{r.json().get('totalCount', '?')} IT offers (free, no key)"


def himalayas(base):
    """Free keyless remote-jobs feed with a country filter."""
    r = requests.get(f"{base}/jobs/api/search",
                     params={"q": "analyst", "country": "PL", "limit": 1}, timeout=30)
    r.raise_for_status()
    return "OK", f"{r.json().get('totalCount', '?')} remote jobs open to Poland"


def jooble():
    """Free partner API aggregating Polish boards; key from jooble.org/api."""
    if not is_set("JOOBLE_API_KEY"):
        return "SKIP", "not configured (optional)"
    r = requests.post(f"{JOOBLE_BASE}/api/{E('JOOBLE_API_KEY').strip()}",
                      json={"keywords": "analyst", "location": "Poland", "page": 1},
                      timeout=30)
    if r.status_code in (401, 403):
        return "FAIL", "key rejected: request a new one at jooble.org/api"
    r.raise_for_status()
    total = r.json().get("totalCount", "?")
    return "OK", f"{total} jobs found for 'analyst' in Poland"


def justjoin():
    """JustJoin.it via a paid Apify actor; only checks the token here so
    the setup check never spends money on a scraping run."""
    if not is_set("APIFY_API_TOKEN"):
        return "SKIP", "not configured (optional)"
    r = requests.get(f"{APIFY_BASE}/users/me",
                     params={"token": E("APIFY_API_TOKEN").strip()}, timeout=30)
    if r.status_code in (401, 403):
        return "FAIL", "token rejected: copy it again from console.apify.com"
    r.raise_for_status()
    return "OK", "Apify token valid (actor bills ~$0.001 per job)"


def nocodb():
    if not (is_set("NOCODB_TOKEN") and is_set("NOCODB_TABLE_ID")):
        return "FAIL", "NOCODB_TOKEN or NOCODB_TABLE_ID not set in .env"
    url = E("NOCODB_URL", "http://nocodb:8080").rstrip("/")
    r = requests.get(f"{url}/api/v2/tables/{E('NOCODB_TABLE_ID').strip()}/records",
                     headers={"xc-token": E("NOCODB_TOKEN").strip()},
                     params={"limit": 1}, timeout=15)
    if r.status_code in (401, 403):
        return "FAIL", "token rejected: create a new API token in NocoDB"
    if r.status_code == 404:
        return "FAIL", "table not found: check NOCODB_TABLE_ID"
    r.raise_for_status()
    return "OK", "tracker table reachable"


def telegram():
    if not (is_set("TELEGRAM_TOKEN") and is_set("TELEGRAM_CHAT_ID")):
        return "FAIL", "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in .env"
    base = f"{TG_BASE}/bot{E('TELEGRAM_TOKEN').strip()}"
    r = requests.get(f"{base}/getMe", timeout=30)
    if r.status_code == 401:
        return "FAIL", "token rejected: copy it again from @BotFather"
    r.raise_for_status()
    bot = r.json().get("result", {}).get("username", "?")
    if "--send" in sys.argv:
        s = requests.post(f"{base}/sendMessage",
                          json={"chat_id": E("TELEGRAM_CHAT_ID").strip(),
                                "text": "Job agent online - test message"}, timeout=30)
        if s.status_code == 400:
            return "FAIL", "bot works but chat ID is wrong, or you never pressed Start"
        s.raise_for_status()
        return "OK", f"bot @{bot}; test message sent"
    return "OK", f"bot @{bot} (add --send to send a test message)"


print("AI Job Agent - setup check")
check("Files", files)
check("Reed API", reed)
check("Adzuna API", adzuna)
check("solid.jobs API", lambda: solidjobs(SOLID_BASE))
check("Himalayas API", lambda: himalayas(HIMALAYAS_BASE))
check("Jooble API", jooble)
check("JustJoin (Apify)", justjoin)
check("AI provider", ai)
check("NocoDB tracker", nocodb)
check("Telegram", telegram)
print("All required checks passed." if failures == 0 else f"{failures} check(s) failed - see hints above.")
sys.exit(1 if failures else 0)
