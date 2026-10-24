# LeadReach Copywriter

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Writes one short, personal outreach email per lead (to its primary published contact) using the ASI:One LLM, grounded only in the brand's own words, the filled-in brand profile and the lead's evidence-backed hooks. A deterministic validator checks every draft: length, subject, unsubscribe line, sender and brand name, no placeholders, no prices or social proof unless the brand profile allows them, and no product claims the brand never made. A failing draft is regenerated once; remaining problems are returned as warnings. User style rules (e.g. "make them shorter") are applied.

**Message:** `ToolRequest(tool="draft_emails", args_json='{"profile": {...}, "brief": "...", "style_rules": [], "icp": {...}, "leads": [...], "contacts": [...], "reviews": [...]}')` → `ToolResponse(ok, result, error)`; `result` = `{"drafts", "skipped"}`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Limits:** leads without a contact, or whose LLM call fails, are skipped with a reason (no draft is ever written without the LLM). Drafts only; it never sends email.

**Keywords:** cold email writer, outreach copy, personalised email, sales email
