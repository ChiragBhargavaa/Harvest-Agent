# LeadReach Contact Finder

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Finds the published contact emails and phone numbers of local businesses by politely crawling up to 5 pages of their own websites (robots.txt respected, ≤1 request/s per site), plus emails tagged on OpenStreetMap. Checks each email domain's MX record, and labels every contact with a role, a confidence (high / medium / low) and its source URL. Never guesses email patterns; the LLM is never a source of emails.

**Message:** `ToolRequest(tool="find_contacts", args_json='{"leads": [...], "max_pages_per_site": 5}')` → `ToolResponse(ok, result, error)`; `result` = `{"contacts", "skipped"}` (skip reasons: no_website, banned_host, robots_disallowed, unreachable, no_email_found, mx_failed). Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Limits:** only what a business publishes on its own site or on OSM; no forms, logins or captchas; directories and social media are never crawled.

**Keywords:** contact finder, email finder, business email, MX check, lead enrichment
