# LeadReach Lead Finder

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Finds local businesses in a city that match a brand's ideal-customer profile. Uses OpenStreetMap (Nominatim + Overpass) and ASI:One web search; every website suggested by search is verified by crawling before it counts. Returns name, address, website, phone and source for each lead, plus a phone-only call list.

**Message:** `ToolRequest(tool="find_leads", args_json='{...}')` → `ToolResponse(ok, result, error)`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Keywords:** local business search, places, OpenStreetMap, find businesses in city, lead generation
