# HANDOFF — LeadReach: finish setup and deploy to Agentverse

**For:** the next coding agent (grokbot). **From:** Claude Code session, 24 Oct 2026.
**Read first:** `CLAUDE.md` (hard rules), then `HACKATHON_README.md` (spec + verified Fetch.ai reference). This file only covers
what's done, what's next, and how to ship.

---

## 1. What this project is
**LeadReach** is a Fetch-A-Thon 3.0 entry: an approval-first lead finder for **brands** that sell to local businesses. It is **not
for schools**; an older spec said that and was wrong. In one ASI:One chat a brand says what it sells and who it wants in a city
(e.g. "I sell organic coffee beans, find cafés in Jaipur"). LeadReach then:
1. builds an ideal-customer profile;
2. finds local businesses;
3. finds their published emails;
4. qualifies each lead with website evidence;
5. drafts emails.

It stops for the user's approval card at every step.

**Lead sources:**
- OpenStreetMap: Nominatim + Overpass (free, no key);
- the ASI:One LLM with `web_search`.

There is **no Google Places key**. Do **not** use Fetch.ai's "Google Maps Places Agent": it is flagged unresponsive, and the user
rejected it.

## 2. Current state (verified)
- 6 uAgents in `agents/`, each runnable alone (`python agents/<file>.py`). Ports 8001–8006.
- `uagents==0.26.1`, `uagents-core==0.4.12`, Python 3.13 in `.venv`. Deps are pinned in `pyproject.toml`, locked in `uv.lock`, and
  exported to `requirements.txt`.
- `.venv/bin/python agents/mock_test.py` → **exit 0, 20/20 checks pass** (offline: mocked LLM, OSM and fetch).
- `ruff check agents scripts` is clean. All 6 agents boot locally with `MAILBOX=false`.
- Live OSM verified through `agents/geo.py`: Jaipur `amenity=cafe` → 86 businesses, 8 with a website, 2 with an email.
- `.env` exists with 6 generated seeds and the derived specialist addresses. **`ASI1_API_KEY` is empty.** `.env` is gitignored.
  **Never commit it, print it, or regenerate the seeds:** the seeds fix the addresses below.
- Git: local repo on `main`, 7 commits, **no remote yet**.

| Agent | File | Name | Address |
|---|---|---|---|
| Orchestrator (Chat Protocol + cards) | `orchestrator.py` | `leadreach` | `agent1q0nlrk0hsqgn9l85z8thxmr5tltjqxeqsmjw4zzme389dcw7zkudjq3lk23` |
| Lead Finder ✅ implemented | `lead_finder.py` | `leadreach-finder` | `agent1q06nvrv65ewr9g62te9xrhqgc7hu8kc3823rrvtttu33nsk4krek2w4cz0d` |
| Contact Finder (shell) | `contact_finder.py` | `leadreach-contacts` | `agent1qdr009c63xpzznwt07k62vxg5fzpr3kf89s65ckz0rjda2ls3s4evf2v4c8` |
| Lead Analyst (shell; `site_signals` done) | `lead_analyst.py` | `leadreach-analyst` | `agent1qd8k32878hmz7jc4cw0cr6mv9q6eqa24xrac6cqxeg0c0kdjf4guj05wpl9` |
| Copywriter (shell) | `copywriter.py` | `leadreach-writer` | `agent1q2ttlc7yh8dwy4vj8knr6jhmguy9pcp0a8zawtwnf3up26zgdq6qsaech9t` |
| Sender (shell, stretch) | `sender.py` | `leadreach-sender` | `agent1q007hz3k52l3pjuwxxfrn28a6xv768alq5jygldx3x7ae6had0d47gtw337` |

"Shell" means the agent runs and replies `ToolResponse(ok=False, "not implemented yet")`. The orchestrator turns that into a
**retry / skip** message.

**Not verified yet:**
- ASI:One web-search quality: does it return real URLs?
- How the cards render in real ASI:One.
- Whether consecutive messages (a progress text, then a card) both show.
- Whether `sender` stays stable across one ASI:One chat.
- The mailbox connection.

## 3. Hard rules (summary; full list in `CLAUDE.md`)
- Never invent brand facts, businesses, emails or evidence.
  - **Emails only from crawled pages or OSM tags, never from the LLM.**
  - LLM-suggested websites count only after `crawl.name_matches` verifies them.
  - Every ❓ in `config/brand_profile.yaml` is for Chirag to fill in. Ask him; don't guess.
- `DRY_RUN=true` by default. Demos use `TEST_REDIRECT_TO`. Never email real businesses from tests.
- Respect robots.txt, the Nominatim policy (≤1 request/s, a real User-Agent) and the Overpass policy (no parallel requests).
  Banned hosts are listed in `crawl.BANNED_HOSTS`.
- Specialists are built with `common.make_specialist`. Handlers never raise.
- uAgents Models are pydantic v1 (`.dict()`).
- **Run `.venv/bin/python agents/mock_test.py` (exit 0) before claiming anything works.** Extend it when you add behaviour.
- Keep `README.md` (the submission README) current: both badges verbatim, plus the agent table with name, address and profile URL.
- Small, descriptive commits.

## 4. Task list, in order

### A. Unblock (needs Chirag)
1. Chirag creates an ASI:One API key (asi1.ai → Developer → API Keys) and puts `ASI1_API_KEY=...` in `.env` himself.
2. Run `.venv/bin/python scripts/probe_sources.py Jaipur cafe` and report the numbers.
   - **GO** (≥3 verified websites): keep web search on.
   - **NO-GO:** set `ASI1_WEB_SEARCH=false`, tell Chirag lead counts will be lower, and update `HACKATHON_README.md` §3.13 with the
     measured result.
3. Ask Chirag for the demo brand, city and business types, then fill `config/brand_profile.yaml`.
4. Optional: `brew install uv`, then use `uv sync` / `uv run`.

### B. First live run (M0 acceptance + M1)
1. `.venv/bin/python agents/local_test.py "I sell organic coffee beans - find cafés in Jaipur"`: a real LLM run in the terminal.
   Fix prompt or parsing issues in `agents/llm.py`.
2. Mailbox:
   - Start `lead_finder.py` and `orchestrator.py` (with `MAILBOX=true`, the default from `.env`).
   - Each prints an **Agent Inspector** link. **Chirag** opens it in the browser → **Connect** → **Mailbox**. This is a manual
     browser step. Do it for all 6 agents once they run.
3. In ASI:One (Agentverse → the orchestrator → **Chat with Agent**), run the flow. Check:
   - the cards render;
   - "remove 2" works;
   - logs show `user agent1...`. Confirm the sender address is stable. If it isn't, set `OWNER_MODE=true`.
4. Record the results in `HACKATHON_README.md` (turn ⚠️ into ✅/❌).

### C. Implement the remaining steps (spec in `HACKATHON_README.md` §3.7–3.8, §3.12–3.13)
- **M2 `contact_finder.find_contacts`:**
  - pipeline: `PoliteFetcher.crawl_site` → `extract_emails` / `extract_phones` → `mx_ok`;
  - confidence rules per §3.8; include OSM `osm_emails`;
  - return `{"contacts": [...], "skipped": [...]}`;
  - crawl leads concurrently across domains (≤5 at a time); the fetcher already rate-limits per domain.
- **M3 `lead_analyst.qualify_leads`:** crawl the site, run `site_signals`, call `llm.qualify(...)`, then fall back to a
  signals-only summary on LLM failure.
- **M4 `copywriter.draft_emails`:** call `llm.draft_email` per lead with the deterministic validator from §3.12 (regenerate
  once, then `warnings`).
  - Also implement `edit_draft` in the orchestrator: a general instruction becomes a `style_rules` entry after confirmation.
- **M5:** `add_lead` / `import_list` in the orchestrator. They are currently answered with "coming soon".
- **M6 (stretch) `sender.send_emails`:**
  - Resend, using `@agent.on_schedule(..., tz="Asia/Kolkata")` for the send window;
  - a cap, a suppression list, and the low-confidence approval invariant (§3.4).
- **Stretch:** Payment Protocol (§4.6, §3.20).
- **For each step:** extend `mock_test.py`, run it, run ruff, commit.

### D. Publish the code
1. Confirm with Chirag, then: `gh repo create leadreach --public --source=. --remote=origin --push`. The `gh` CLI is logged in as
   ChiragBhargavaa.
2. Before pushing, run `git status` and `git check-ignore .env`. Make sure no secrets are tracked.

### E. Deploy (agents must stay online until results on **28 Oct 2026, 12:15 IST**; a laptop is not enough)
- **Mailbox agents** need outbound internet only: no public port, no web server.
- **Option 1 (recommended): one small VPS or Render Background Worker** running all 6 agents.
  - Add `scripts/run_all.py`, a supervisor that starts `agents/*.py` as 6 subprocesses and restarts any that exit. Agents use
    separate ports, so they can share a host.
  - Running all agents in one `Bureau` with `mailbox=True` is **unverified**; test it before relying on it.
  - Render:
    - build `pip install -r requirements.txt`, start `python scripts/run_all.py`;
    - set `PYTHON_VERSION=3.13` and every `.env` variable in the dashboard, with the **same seeds**.
    - ⚠️ Background workers may not be on Render's free plan. Check pricing and ask Chirag before paying for anything.
- **Option 2:** Agentverse hosted agents (one agent per project, no `Bureau`, allowed imports only). Shared modules would have to
  be inlined into each file (§4.7). That's more work, so only do it if Option 1 fails.
- After deploying:
  - check the logs show all 6 addresses unchanged (they must match the table above);
  - the mailboxes stay connected because the addresses didn't change;
  - re-run the full ASI:One flow.

### F. Agentverse profiles + ranking (§4.7)
For each agent, on Agentverse:
- short name ≤20 chars (as in the table);
- a handle (e.g. `@leadreach`);
- an avatar;
- the README from `agents/profiles/<agent>.md` (already has badges, use cases, limitations and keywords; it's published through
  `readme_path`);
- keep the agents **active**.

Test discoverability in ASI:One without @-mentioning the agent, e.g. "find cafés in Jaipur to sell coffee beans to". Then copy
each **profile URL** into the `README.md` table.

### G. Submission (§2.6, §8)
1. Run the full flow in ASI:One on the deployed agents and copy the **Shared Chat URL**.
2. Chirag records a **3–5 min demo video** (script in §3.19).
3. Fill in the `README.md` links table.
4. Submit via **asi1.ai/festival/fetchathon3/dashboard → Project Submission Agent**: the chat URL, profile URLs, repo URL, video,
   and a description (problem, target users, outcome).
5. Confirm `DRY_RUN=true` / redirect on the deployed agents, so judges testing it never email real businesses.

## 5. Useful commands
```bash
.venv/bin/python agents/mock_test.py                  # offline test, must exit 0
.venv/bin/ruff check agents scripts                   # lint
.venv/bin/python scripts/probe_sources.py Jaipur cafe # lead-source go/no-go (needs ASI1_API_KEY)
.venv/bin/python agents/local_test.py "<brief>"       # live loop in terminal (real LLM + network)
bash setup.sh                                          # safe to re-run: keeps .env/seeds, re-derives addresses
# deps changed?  uv lock && uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
```

## 6. Key files
- `agents/orchestrator.py`: intent (card JSON → fast path → `llm.parse_intent`), the `run_from` gate loop, `checkpoint` cards,
  handlers.
- `agents/state.py`: states, gates, campaign defaults, audit log.
- `agents/cards.py`: Interactive Card builders. Each narration must make sense on its own, in case the card doesn't render.
- `agents/lead_finder.py`, `geo.py`, `crawl.py`, `llm.py`: the lead pipeline and its prompts.
- `config/brand_profile.yaml`: brand facts, all ❓ until Chirag fills them in.
- `HACKATHON_README.md`: the spec. Update it in the same change whenever you change a contract.
