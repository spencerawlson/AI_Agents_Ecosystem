# 03 — Project Tracker (database)

One database named **Projects**. A project belongs to one client and
spawns the invoices that bill it.

## Properties

| Property | Type | Options / notes |
|----------|------|-----------------|
| Project | Title | |
| Client | Relation | → Clients |
| Status | Select | Not started · Active · In review · Delivered · On hold |
| Type | Select | Design · Writing · Development · Consulting · Other |
| Start date | Date | |
| Deadline | Date | |
| Fee | Number ($) | Agreed project fee |
| Invoiced | Rollup | From Invoices: sum of Amount (all statuses) |
| Paid | Rollup | From Invoices: sum of Amount where Status = Paid |
| Balance due | Formula | `prop("Fee") - prop("Paid")` |
| Progress | Percent | Manual 0–100, shown in the HQ view |
| Deliverables | Text | What "done" means, agreed up front |
| Invoices | Relation | → Invoices database |
| Notes | Text | |

## Views

1. **Board** (grouped by Status) — the delivery pipeline.
2. **Deadlines** (calendar by Deadline) — what's due this week.
3. **Unpaid balances** (table) — filter: Balance due > 0 and Status
   is not Not started. Sort by Deadline.

## Page template inside the database

New-project page body:

```
## Brief
What the client asked for, in their words.

## Deliverables & milestones
- [ ] Milestone 1 —
- [ ] Milestone 2 —

## Files & links
-

## Log
- YYYY-MM-DD —
```

## Sample entries

- `[Sample] Website refresh` — Northwind Studio, Active, Fee $2,400
- `[Sample] Menu design` — Café Lumière, In review, Fee $600
