"""Probe the configured LLM endpoint before starting the app.

Reads .env-style values from the environment (run.bat / uvicorn commands load
them the same way). Verifies, in order:
  1. key accepted by the endpoint (GET /models)
  2. the configured model exists (or lists valid ones)
  3. one tiny non-streaming chat call works
  4. one streaming call yields deltas and terminates with [DONE]

Usage: .venv/Scripts/python.exe scripts/probe_llm.py
Exit code 0 = everything green; non-zero = first failing step.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env the same way run.bat does (fill-only).
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from app.model_config import load_model_config  # noqa: E402

import httpx  # noqa: E402

cfg = load_model_config()
if not cfg.enabled:
    print("FAIL LLM_API_KEY is empty — local demo mode, nothing to probe")
    sys.exit(1)
masked = f"****{cfg.api_key[-4:]}"
print(f"endpoint: {cfg.base_url}  model: {cfg.model}  key: {masked}")

steps = 0
with httpx.Client(timeout=30) as client:
    headers = {**cfg.authorization_header, "Content-Type": "application/json"}

    r = client.get(f"{cfg.base_url}/models", headers=headers)
    steps += 1
    if r.status_code != 200:
        print(f"FAIL /models -> {r.status_code}: {r.text[:200]}")
        print("     (401 invalid key / 404 wrong base_url — check both)")
        sys.exit(2)
    model_ids = [m.get("id") for m in r.json().get("data", [])]
    print(f"PASS /models -> {len(model_ids)} models")

    steps += 1
    if cfg.model in model_ids:
        print(f"PASS configured model '{cfg.model}' is listed")
    else:
        print(f"WARN configured model '{cfg.model}' NOT in {model_ids} — trying anyway")

    steps += 1
    r = client.post(
        cfg.chat_completions_url,
        headers=headers,
        json={"model": cfg.model, "messages": [{"role": "user", "content": "回复：ok"}],
              "max_tokens": 8},
    )
    if r.status_code != 200:
        print(f"FAIL chat -> {r.status_code}: {r.text[:200]}")
        sys.exit(3)
    print(f"PASS chat -> {r.json()['choices'][0]['message']['content']!r}")

    steps += 1
    got, saw_done = [], False
    with client.stream(
        "POST", cfg.chat_completions_url, headers=headers,
        json={"model": cfg.model,
              "messages": [{"role": "user", "content": "数到三"}],
              "max_tokens": 32, "stream": True},
    ) as resp:
        if resp.status_code != 200:
            print(f"FAIL stream -> {resp.status_code}: {resp.read()[:200]!r}")
            sys.exit(4)
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            piece = (json.loads(payload).get("choices") or [{}])[0].get("delta", {}).get("content")
            if piece:
                got.append(piece)
    print(f"PASS stream -> {len(got)} deltas, [DONE]={saw_done}, text={(''.join(got))[:60]!r}")

print(f"{steps}/{steps} steps passed — app is ready for this endpoint")
