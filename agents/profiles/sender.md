# LeadReach Sender

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Sends approved outreach emails through Resend within a daily cap and business-hours window. Dry-run and test-redirect are on by default; unsubscribes are honoured permanently.

**Message:** `ToolRequest(tool="send_emails", args_json='{...}')` → `ToolResponse(ok, result, error)`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Keywords:** email sending, outreach automation, daily cap, unsubscribe
