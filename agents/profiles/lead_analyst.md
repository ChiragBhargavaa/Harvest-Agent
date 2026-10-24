# LeadReach Lead Analyst

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Reviews each business's website and scores how well it fits a brand's offer (0-100) with a short summary and up to three outreach hooks. It crawls the site politely, reads website signals (title, description, menu / online ordering / events / catering mentions, latest year, app links), then asks the ASI:One LLM for hooks. A hook is kept only if its evidence snippet really appears on a crawled page, and it carries that page's URL.

**Message:** `ToolRequest(tool="qualify_leads", args_json='{"icp": {...}, "leads": [...], "contacts": [...]}')` → `ToolResponse(ok, result, error)`; `result` = `{"reviews"}`, one per lead, best fit first. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Limits:** if the LLM fails, the review is scored from website signals only (no hooks, marked as fallback); leads without a reachable website get a signals-only review with the reason.

**Keywords:** lead qualification, website review, fit score, sales intelligence
