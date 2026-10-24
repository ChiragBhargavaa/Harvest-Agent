# LeadReach - local B2B lead finder for brands

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

**LeadReach finds local businesses your brand should sell to, their published contact emails, evidence-based reasons to reach out, and drafts personal outreach emails - stopping for your approval at every step. It does not send email in this demo.**

## Use cases
- A coffee-bean brand finding cafés and bakeries in Jaipur
- A packaging supplier finding restaurants and cloud kitchens in Pune
- A SaaS tool for salons finding salons and spas in Bengaluru

## How to interact
- *"I sell organic coffee beans - find cafés and bakeries in Jaipur"*
- At each checkpoint: **approve**, **remove 2, 5**, **redo** with feedback, **show lead 3**, **call list**
- On drafts: **approve**, **skip**, **approve all**, or an edit like *"make it warmer"* (a general edit can become a style rule for all drafts after you confirm)
- Anytime: **status**, **settings**, **pause**, **resume**, **log**, **retry** / **skip** after a failed step; *"stop asking me about contacts"*

## How it works
The ASI:One LLM turns your brief into an ideal-customer profile, then a multi-agent pipeline on Agentverse runs: Lead Finder (OpenStreetMap + ASI:One web search, websites verified by crawling) → Contact Finder (polite website crawl, MX check, confidence + source URL) → Lead Analyst (fit score + evidence-backed hooks) → Copywriter (ASI:One drafts + validator). Every checkpoint is an Interactive Card, and every action is written to an audit log.

## Limitations
- Only uses publicly published contact details; never guesses email addresses.
- Coverage depends on OpenStreetMap and public websites; small businesses without a website may only have a phone.
- Sending is not part of this demo (dry-run only): the workflow ends at approved drafts. Adding leads by hand, importing lists and reply classification are not part of this demo yet.

**Keywords:** lead generation, local business leads, B2B outreach, sales prospecting, find businesses in city, cold email, contact finder, Google Maps alternative, OpenStreetMap, India
