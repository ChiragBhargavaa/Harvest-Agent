# Harvest — project description

**Harvest** is an approval-first lead finder and outreach copilot. A brand says what it sells and who it wants to reach in a city — for example, *"I sell a new trimmer — find salons in Jodhpur"*. The whole workflow runs inside one ASI:One chat. There is no custom frontend.

You talk to **one** agent. Behind that chat, **six** uAgents on Agentverse cooperate. Harvest Agent (the orchestrator) understands the brief, builds an ideal-customer profile, shows Interactive Cards at every checkpoint, and calls the specialists in order. Each specialist does one job and replies with a result the user can approve, edit, remove, redo, retry, skip, or pause.

**The six agents**

1. **Harvest Agent** (orchestrator) — talks to the brand in ASI:One (Chat Protocol + Interactive Cards). Builds the ideal-customer profile, runs the approval gates, applies draft edits, keeps an audit log, and can sell a verified lead pack over the Payment Protocol.
2. **leadreach-finder** — finds matching local businesses from OpenStreetMap (Nominatim + Overpass) and ASI:One web search, then verifies candidate websites by crawling.
3. **leadreach-contacts** — finds published emails and phones by politely crawling each business's own site, checks MX, and returns a role, confidence, and source URL for every contact.
4. **leadreach-analyst** — scores each lead and writes evidence-backed hooks from the website (signals-only fallback if the page cannot be read).
5. **leadreach-writer** — drafts a short personal outreach email per lead (ASI:One LLM) and runs a deterministic validator so prices, claims, and social proof are never invented.
6. **leadreach-sender** — placeholder only. It runs and answers, but sending is not part of this demo (`DRY_RUN` only; nothing is ever emailed).

Emails come only from a crawled page or an OpenStreetMap tag — never from the LLM. Websites suggested by web search count only after a crawl confirms the business. The demo ends at approved drafts. Optionally, saying **buy lead pack** requests a Payment Protocol sale of the verified contacts and their source URLs (demo verification, not on-chain).

---

## Summary

### 1. Problem solved

Small brands that sell to local businesses (cafés, salons, gyms, retailers) spend hours per lead finding the right businesses, a real published email, and a reason to write. Lead databases barely cover Indian SMBs. Autopilot AI outreach tools invent contacts or send the wrong email without asking. Harvest does the research and drafting, then waits for approval at every step.

### 2. Target users

Founders and small sales teams of **B2B brands selling locally** — not consumer-to-consumer, and not schools. The hosted demo is a grooming-products brand selling a new trimmer to salons and barber shops in Jodhpur. The same flow works for a coffee roaster selling to cafés, a packaging supplier selling to restaurants, or a SaaS tool for gyms. The public demo starts at most 3 new campaigns per person and 20 in total each IST day.

### 3. Outcome of the agent

In one ASI:One conversation the brand gets, and can approve at each step: an ideal-customer profile; a sourced lead list with verified websites; published contacts with source URL, role, confidence, and MX check; evidence-based reviews (fit score + hooks from the business's own site); and personal email drafts that pass the validator. Sending is off. Nothing is invented, and nothing goes out without an explicit yes.
