# LeadReach Lead Finder

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Finds local businesses in a city that match a brand's ideal-customer profile. Uses OpenStreetMap (Nominatim + Overpass) and up to 3 ASI:One web searches run concurrently; every website suggested by search is verified by crawling (the page must load and show the business name) before it counts. Returns up to 15 leads with a website or published email (name, address, website, phone, source), plus a phone-only call list and source notes.

**Message:** `ToolRequest(tool="find_leads", args_json='{"icp": {...}, "limit": 15, "exclude_ids": []}')` → `ToolResponse(ok, result, error)`; `result` = `{"leads", "call_list", "source_notes", "city"}`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Limits:** coverage depends on OpenStreetMap and public websites; web-only businesses without a verified site are dropped; never scrapes Google Maps, Justdial, IndiaMART, Zomato, Swiggy or social media. If one source fails, results from the other are returned with a note.

**Keywords:** local business search, places, OpenStreetMap, find businesses in city, lead generation
