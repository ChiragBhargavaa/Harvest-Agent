"""Contact Finder agent: `find_contacts` - published emails/phones for each lead (polite crawl + MX check).

M2 work: crawl.PoliteFetcher.crawl_site -> crawl.extract_emails / extract_phones -> crawl.mx_ok ->
confidence (high: role/owner email on the business's own domain; medium: generic address on own domain or
free-mail published on its own site; low: only found on a third-party page). Never guess email patterns.

Run:  python agents/contact_finder.py
"""

from common import make_specialist


async def find_contacts(args: dict) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    raise NotImplementedError("find_contacts is not implemented yet (milestone M2)")


agent = make_specialist(
    name="leadreach-contacts",
    seed_env="CONTACT_SEED",
    port_env="CONTACT_PORT",
    default_port=8003,
    profile="contact_finder.md",
    description="Finds the published contact emails and phone numbers of local businesses by politely crawling their websites.",
    tools={"find_contacts": find_contacts},
)

if __name__ == "__main__":
    agent.run()
