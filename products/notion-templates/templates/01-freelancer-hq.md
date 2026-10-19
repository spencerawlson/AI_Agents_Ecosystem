# 01 — Freelancer HQ (home page)

The page every buyer lands on. Build it as a normal Notion page named
**Freelancer HQ** with the layout below. Every block is a *linked
view* of one of the pack's databases, so the HQ is live the moment
the databases exist — no manual upkeep.

## Page structure

```
# Freelancer HQ
> Your whole practice on one page. Delete the [Sample] entries in
> each database when you're ready to start.

## 📅 Today
[Linked view: Tasks database — filter: Due = Today OR Status = Doing,
 sort by Due. Gallery off, table compact.]

## 🚧 Active projects
[Linked view: Projects — filter: Status is Active or In review.
 Show: Project, Client, Deadline, Progress. Board grouped by Status.]

## 💸 Money in
[Linked view: Invoices — filter: Status is Sent or Overdue.
 Show: Invoice, Client, Amount, Due Date, Flag. Sort by Due Date.]

## 🧲 Pipeline
[Linked view: Clients — filter: Status is Lead or Proposal sent.
 Show: Client, Contact, Next step, Follow-up date.]

## 🔗 Quick links
- ➕ New client  (button → creates a page in Clients)
- ➕ New project (button → creates a page in Projects)
- ➕ New invoice (button → creates a page in Invoices)
- 📝 Proposal template (link to the page in template 05)
- 📆 Weekly planner (link to the page in template 05)
```

## Build notes

- Notion *Buttons* (the ➕ links) are added with `/button`: action
  "Add page to" the matching database, with Status preset to the
  first pipeline stage. If the buyer's Notion plan lacks buttons,
  the fallback in the setup guide is a plain link to each database.
- Keep the HQ to these four views. The #1 reason freelancers abandon
  dashboards is clutter; this page answers four questions only:
  *What do I do today, what's in flight, who owes me money, who do I
  chase?*
