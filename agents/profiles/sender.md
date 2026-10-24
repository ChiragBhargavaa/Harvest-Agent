# LeadReach Sender

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Placeholder for sending approved outreach emails. **Sending is not part of this demo:** `DRY_RUN=true`, and `send_emails` currently replies `ok=False` ("not implemented yet"), so nothing is ever sent. The orchestrator then offers **skip** to finish the campaign. Planned (not built): Resend delivery with a daily cap, a business-hours window, test redirect and permanent unsubscribes.

**Message:** `ToolRequest(tool="send_emails", args_json='{...}')` → `ToolResponse(ok=False, error="... not implemented yet ...")`. Part of the LeadReach multi-agent system (orchestrator: `leadreach`).

**Keywords:** email sending, outreach automation, dry run
