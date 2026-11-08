# 02 — Client CRM (database)

One database named **Clients**. Every other template links back to
it, so a buyer always sees a client's projects and invoices from the
client's own page.

## Properties

| Property | Type | Options / notes |
|----------|------|-----------------|
| Client | Title | Company or person name |
| Status | Select | Lead · Proposal sent · Active · Past · Archived |
| Contact name | Text | Main human |
| Email | Email | |
| Website | URL | |
| Rate type | Select | Hourly · Per project · Retainer |
| Rate | Number ($) | Their agreed rate |
| Source | Select | Referral · Etsy · Website · LinkedIn · Cold outreach · Other |
| First contact | Date | |
| Follow-up date | Date | Drives the HQ pipeline view |
| Next step | Text | One line: the next action with this client |
| Lifetime value | Rollup | From Invoices: sum of Amount where Status = Paid |
| Projects | Relation | → Projects database |
| Invoices | Relation | → Invoices database |
| Notes | Text | Anything that doesn't fit elsewhere |

## Views

1. **All clients** (table) — sort by Status, then Client.
2. **Pipeline** (board, grouped by Status) — the sales view.
3. **Follow up** (table) — filter: Follow-up date is today or past,
   Status is Lead or Proposal sent. This is the money view: nobody
   falls through.

## Sample entries (mark with [Sample], buyers delete them)

- `[Sample] Northwind Studio` — Active, Retainer, $1,500
- `[Sample] Café Lumière` — Proposal sent, follow-up in 2 days
- `[Sample] J. Alvarez Coaching` — Lead, Referral
