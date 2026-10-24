# LeadReach - local B2B lead finder for brands

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

**LeadReach finds local businesses your brand should sell to, their published contact emails, evidence-based reasons to reach out, and drafts personal outreach emails - stopping for your approval at every step.**

## Use cases
- A coffee-bean brand finding cafés and bakeries in Jaipur
- A packaging supplier finding restaurants and cloud kitchens in Pune
- A SaaS tool for salons finding salons and spas in Bengaluru

## How to interact
- *"I sell organic coffee beans - find cafés and bakeries in Jaipur"*
- At each checkpoint: **approve**, **remove 2, 5**, **redo** with feedback, **show lead 3**
- Anytime: **status**, **settings**, **pause**, **resume**, **log**; *"stop asking me about contacts"*

## How it works
A multi-agent pipeline on Agentverse: Lead Finder (OpenStreetMap + ASI:One web search) → Contact Finder (polite website crawl, MX check) → Lead Analyst (evidence-backed fit score) → Copywriter (ASI:One drafts + validator) → Sender (dry-run by default). Every checkpoint is an Interactive Card.

## Limitations
- Only uses publicly published contact details; never guesses email addresses.
- Coverage depends on OpenStreetMap and public websites; small businesses without a website may only have a phone.
- Never sends without approval; dry-run is on by default.

**Keywords:** lead generation, local business leads, B2B outreach, sales prospecting, find businesses in city, cold email, contact finder, Google Maps alternative, OpenStreetMap, India
