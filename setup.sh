#!/usr/bin/env bash
# One-shot setup: deps (uv, or venv+pip fallback) + .env with fresh seeds + derived agent addresses + local git repo.
# Usage:  bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

# ---- 1. Dependencies ----
if command -v uv >/dev/null 2>&1; then
  echo "==> Installing with uv"
  uv sync
else
  echo "==> uv not found (recommended: brew install uv). Falling back to venv + pip."
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)'; then PY="$c"; break; fi
  done
  [ -z "$PY" ] && { echo "Need Python 3.10-3.13 (brew install python@3.13)"; exit 1; }
  [ -d .venv ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
PYBIN=.venv/bin/python
$PYBIN -c "from uagents_core.contrib.protocols.chat import chat_protocol_spec as s; import importlib.metadata as m; print('==> uagents', m.version('uagents'), '|', s.name, s.version)"

# ---- 2. .env with fresh random seeds ----
setenv() {  # setenv KEY VALUE  (portable in-place edit for macOS + Linux)
  $PYBIN - "$1" "$2" <<'PY'
import re, sys, pathlib
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); text = p.read_text()
text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M) if re.search(rf"^{key}=", text, re.M) else text + f"\n{key}={value}\n"
p.write_text(text)
PY
}
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  for k in ORCH_SEED FINDER_SEED CONTACT_SEED ANALYST_SEED WRITER_SEED SENDER_SEED; do
    setenv "$k" "$($PYBIN -c 'import secrets; print(secrets.token_hex(32))')"
  done
  read -r -s -p "Paste your ASI:One API key (asi1.ai -> Developer -> API Keys; Enter to skip): " KEY; echo
  [ -n "$KEY" ] && setenv ASI1_API_KEY "$KEY"
  echo "==> .env created (seeds generated; it is gitignored)"
fi

# ---- 3. Specialist addresses, derived from the seeds ----
while read -r seed addr; do
  value=$($PYBIN -c "
from dotenv import dotenv_values; from uagents.crypto import Identity
print(Identity.from_seed(dotenv_values('.env')['$seed'], 0).address)")
  setenv "$addr" "$value"
  echo "==> $addr=$value"
done <<'EOF'
FINDER_SEED LEAD_FINDER_AGENT_ADDRESS
CONTACT_SEED CONTACT_FINDER_AGENT_ADDRESS
ANALYST_SEED LEAD_ANALYST_AGENT_ADDRESS
WRITER_SEED COPYWRITER_AGENT_ADDRESS
SENDER_SEED SENDER_AGENT_ADDRESS
EOF
echo "==> Orchestrator: $($PYBIN -c "
from dotenv import dotenv_values; from uagents.crypto import Identity
print(Identity.from_seed(dotenv_values('.env')['ORCH_SEED'], 0).address)")"

# ---- 4. Local git repo (no automatic push) ----
if [ ! -d .git ]; then
  git init -q -b main
  git add -A
  git commit -q -m "LeadReach starter" && echo "==> git repo initialised"
fi
echo
echo "==> Done. Next:"
echo "   .venv/bin/python agents/mock_test.py        # offline test, no keys needed"
echo "   .venv/bin/python scripts/probe_sources.py   # checks OSM + ASI:One web search with your key"
echo "   To publish: gh repo create leadreach --public --source=. --remote=origin --push"
