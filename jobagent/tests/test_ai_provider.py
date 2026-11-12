"""Offline test for the AI provider switch in finder.py.

Run from repo root:  python3 .test_mistral_offline.py
No network, no real API keys: HTTP calls are replaced with fakes.
"""
import importlib.util
import json
import os
import sys
import types

# finder.py imports requests; the host has no need for it, so stub it out.
# Each test replaces finder.requests.post with its own fake anyway.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        post=None, get=None, RequestException=Exception)


def load_finder(env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "finder_test", "../app/finder.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


JOB = {"title": "SOC Analyst", "company": "Acme", "country": "GB",
       "location": "London", "description": " Monitor the SOC. " * 5}
MASTER = "# Candidate\nPython, Splunk, 1 year experience."

failures = []


def check(name, cond, detail=""):
    print(f"  {name:<45} {'PASS' if cond else 'FAIL'} {detail}")
    if not cond:
        failures.append(name)


# --- 1. Mistral provider sends the right request shape ----------------------
os.environ.update({
    "AI_PROVIDER": "mistral",
    "MISTRAL_API_KEY": "test-key",
    "SCORING_MODEL": "mistral-small-latest",
})
finder = load_finder({"AI_PROVIDER": "mistral", "MISTRAL_API_KEY": "test-key",
                      "SCORING_MODEL": "mistral-small-latest"})

captured = {}


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    captured.update(url=url, json=json, headers=headers)
    return FakeResponse({"choices": [{"message": {"content":
            '{"score": 82, "level_fit": "good", "reasons": ["soc"], "gaps": [], "sponsorship_red_flag": false}'}}]})

finder.requests.post = fake_post
result = finder.score_job(JOB, MASTER)

check("mistral: hits /v1/chat/completions",
      captured["url"].endswith("/v1/chat/completions"), captured["url"])
check("mistral: bearer auth header",
      captured["headers"]["Authorization"] == "Bearer test-key")
check("mistral: model passed", captured["json"]["model"] == "mistral-small-latest")
check("mistral: JSON mode requested",
      captured["json"]["response_format"] == {"type": "json_object"})
check("mistral: profile in system prompt",
      "CANDIDATE PROFILE" in captured["json"]["messages"][0]["content"])
check("mistral: advert in user prompt",
      "SOC Analyst" in captured["json"]["messages"][1]["content"])
check("mistral: score parsed", result["score"] == 82, str(result))

# --- 2. Mistral error handling ----------------------------------------------
def fake_post_401(url, **kw):
    return FakeResponse({}, status_code=401)
finder.requests.post = fake_post_401
try:
    finder.score_job(JOB, MASTER)
    check("mistral: 401 raises clear error", False)
except RuntimeError as exc:
    check("mistral: 401 raises clear error", "401" in str(exc), str(exc))

def fake_post_429(url, **kw):
    return FakeResponse({}, status_code=429)
finder.requests.post = fake_post_429
try:
    finder.score_job(JOB, MASTER)
    check("mistral: 429 raises clear error", False)
except RuntimeError as exc:
    check("mistral: 429 raises clear error", "429" in str(exc), str(exc))

# missing key
os.environ.pop("MISTRAL_API_KEY", None)
finder2 = load_finder({"AI_PROVIDER": "mistral", "MISTRAL_API_KEY": "",
                       "SCORING_MODEL": "mistral-small-latest"})
try:
    finder2.score_job(JOB, MASTER)
    check("mistral: missing key -> clear error", False)
except RuntimeError as exc:
    check("mistral: missing key -> clear error", "MISTRAL_API_KEY" in str(exc), str(exc))

# --- 3. Ollama provider unchanged -------------------------------------------
finder3 = load_finder({"AI_PROVIDER": "", "SCORING_MODEL": "qwen2.5:3b"})
captured3 = {}

def fake_post_ollama(url, json=None, timeout=None, **kw):
    captured3.update(url=url, json=json)
    return FakeResponse({"message": {"content":
            '{"score": 42, "reasons": [], "gaps": []}'}})

finder3.requests.post = fake_post_ollama
result3 = finder3.score_job(JOB, MASTER)
check("ollama: still hits /api/chat",
      captured3["url"].endswith("/api/chat"), captured3["url"])
check("ollama: keeps options/num_ctx",
      captured3["json"]["options"]["num_ctx"] == 8192)
check("ollama: score parsed", result3["score"] == 42, str(result3))

# --- 4. Unknown provider rejected -------------------------------------------
finder4 = load_finder({"AI_PROVIDER": "openai"})
try:
    finder4.score_job(JOB, MASTER)
    check("unknown provider -> clear error", False)
except RuntimeError as exc:
    check("unknown provider -> clear error", "AI_PROVIDER" in str(exc), str(exc))

# --- 5. parse_score robustness ----------------------------------------------
raw = 'prefix noise {"score": "88", "sponsorship_red_flag": "true"} suffix'
pr = finder.parse_score(raw)
check("parse: score string -> int clamped", pr["score"] == 88, str(pr["score"]))
check("parse: 'true' as string -> bool", pr["sponsorship_red_flag"] is True)

print()
if failures:
    print(f"{len(failures)} test(s) FAILED")
    sys.exit(1)
print("All offline tests passed.")
