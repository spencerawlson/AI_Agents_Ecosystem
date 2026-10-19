# 04 — Invoice & Income Tracker (database) — Complete tier

One database named **Invoices**. This is the template buyers say
sold them the pack: it always knows who owes what, and it nags
politely via the Flag formula instead of a spreadsheet.

## Properties

| Property | Type | Options / notes |
|----------|------|-----------------|
| Invoice | Title | e.g. `INV-2026-014` |
| Client | Relation | → Clients |
| Project | Relation | → Projects |
| Amount | Number ($) | |
| Issued | Date | |
| Due Date | Date | |
| Paid date | Date | Fill when the money lands |
| Status | Select | Draft · Sent · Paid · Overdue (set by Flag check) · Written off |
| Flag | Formula | see below |
| Days late | Formula | see below |
| Method | Select | Bank transfer · Card · PayPal · Wise · Other |
| Notes | Text | |

## Formulas (Notion formula syntax, paste exactly)

**Flag**
```
if(prop("Status") == "Paid", "✅ Paid",
  if(prop("Due Date") < today() and prop("Status") != "Written off",
     "🔴 Overdue", "🕓 Open"))
```

**Days late**
```
if(prop("Flag") == "🔴 Overdue",
   dateBetween(today(), prop("Due Date"), "days"), 0)
```

## Views

1. **Open invoices** (table) — filter: Status is Sent or Overdue.
   Sort by Due Date. This view is mirrored on the HQ page.
2. **Overdue** (table) — filter: Flag is 🔴 Overdue. Sort by Days
   late, descending. The Monday-morning view.
3. **Income by month** (table) — filter: Status is Paid, group by
   Paid date (month), show sum of Amount. The "how was this month,
   really" view.
4. **This tax year** (table) — filter: Paid date is within the
   current year, sum of Amount visible at the bottom. Accountants
   love this one.

## Sample entries

- `[Sample] INV-2026-011` — Northwind Studio, $800, Paid
- `[Sample] INV-2026-012` — Café Lumière, $600, Sent, due in 5 days
- `[Sample] INV-2026-009` — J. Alvarez Coaching, $450, Overdue
  (shows the 🔴 flag working on day one)

## Buyer note (include on the HQ page)

> Set an invoice to **Overdue** yourself if you prefer the status to
> match the flag; the flag is the source of truth either way.
