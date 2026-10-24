# CLAUDE.md

Project: **LeadReach**, Chirag's Fetch-A-Thon 3.0 entry (uAgents + Agentverse + ASI:One): an approval-first lead finder and
outreach copilot for **brands** that sell to local businesses (not schools).

**Before doing anything, read `HACKATHON_README.md` in full.** It has:
- the hackathon rules, judging criteria and submission flow;
- the verified Fetch.ai API reference (Chat Protocol, Interactive Cards, Payment Protocol, Agentverse, ASI:One web search);
- the LeadReach spec: contracts, state machine, cards, prompts, data sources, guardrails, milestones and the demo script.

Hard rules (details in `HACKATHON_README.md` §1):
- **Versions:** Python 3.10–3.13, `uagents==0.26.1`, `uagents-core==0.4.12` (pinned in `pyproject.toml`, locked in `uv.lock`).
  uAgents Models are **pydantic v1** (`.dict()`, not `.model_dump()`).
- **Tooling:** `uv sync` to install; after changing deps run `uv lock` and
  `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`. Lint with `.venv/bin/ruff check agents scripts`.
- **Facts:** never invent brand facts, businesses, emails or evidence. Emails come only from crawled pages or OSM tags, never
  from the LLM; LLM-suggested websites count only after crawling verifies them. Anything marked ❓ → ask Chirag.
- **Lead sources:** OpenStreetMap (Nominatim + Overpass) + ASI:One web search. Don't use Fetch.ai's unresponsive Google Maps
  Places Agent. Never scrape Google Maps, Justdial, IndiaMART, Zomato, Swiggy, LinkedIn, Facebook or Instagram.
- **Sending:** `DRY_RUN=true` by default; use `TEST_REDIRECT_TO` for demos. Never email real businesses from tests.
- **Secrets:** never commit `.env`, seeds or API keys.
- **Crawling:** respect robots.txt, OSM usage policies and rate limits (`agents/crawl.py`, `agents/geo.py`).
- **Specialists:** build with `common.make_specialist`; always reply with `ToolResponse` (`ok=False` on errors). Handlers never raise.
- **Testing:** run `.venv/bin/python agents/mock_test.py` (exit 0) before saying a change works.
- **Submission README:** `README.md` is the submission README. Keep its badges and agent table (name, address, profile URL) current.
