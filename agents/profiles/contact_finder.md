# LeadReach Contact Finder

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Finds the published contact emails and phone numbers of local businesses by politely crawling their own websites (robots.txt respected, rate-limited), checks MX records, and labels each contact's confidence with its source URL. Never guesses email patterns.

**Message:** `ToolRequest(tool="find_contacts", args_json='{...}')` → `ToolResponse(ok, result, error)`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Keywords:** contact finder, email finder, business email, MX check, lead enrichment
