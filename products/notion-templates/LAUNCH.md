# Launch checklist — both channels

Decision (2026-10-18): launch on **Gumroad and Etsy**, tiers
Starter $19 / Complete $29. Everything hinges on one asset — the
Notion duplicate link — so the build comes first; both listings
afterwards are copy-paste from `LISTING.md`.

## Step 1 — Build the pack (owner, ~1 hour)

Follow `SETUP.md` Part 1 and Part 2 in your Notion:
build the three databases, the HQ page, the page templates, add the
`[Sample]` entries, then *Share to web → Allow duplicate as
template* on the root page.

**Output of this step: the duplicate link.** Send it to Everest (or
drop it into `SETUP.md`) — it unlocks both listings and the final
Etsy PDF.

## Step 2 — Gumroad (owner, ~15 min)

1. Create the account + payout details (yours alone).
2. New product → name and description from `LISTING.md`.
3. Two variants: **Starter $19** and **Complete $29** — or two
   separate products if variants hide the difference; variants are
   cleaner.
4. Post-purchase content: the duplicate link + the "first five
   minutes" steps from `SETUP.md` Part 3.
5. For the Complete tier, the link points at the full workspace;
   for Starter, duplicate the workspace, delete templates 04–05
   from the copy, and use that second link. (Two share links, one
   per tier — build both in Step 1 if you can.)

## Step 3 — Etsy (owner, ~20 min)

1. Open the shop + payout/tax details (yours alone). Check the
   digital-goods VAT obligations noted in `SETUP.md` before
   publishing.
2. Two digital-download listings (Starter, Complete), titles and
   tags from `LISTING.md`.
3. Upload the deliverable PDF for the tier:
   `etsy-deliverable/freelancer-os-access-starter.pdf` /
   `...-complete.pdf` — Everest regenerates these with the real
   duplicate links the moment they exist (the files in the repo are
   the print-ready masters with the link slot marked).
4. Listing fee: $0.20 per listing, 4 months.

## Step 4 — Book the first sale (owner → Everest's books)

When a sale lands, book it the same day so the GUI tells the truth:

```
python3 ecosystem/notion_templates_growth.py --record-revenue 29 --period 2026-10 --source Gumroad
python3 ecosystem/notion_templates_growth.py --record-cost 0.20 --label "Etsy listing fee"
python3 ecosystem/notion_templates_growth.py --pnl
```

## Optional launch lever (owner's call)

Complete at $19 for the first 10 sales to gather reviews, then $29
(noted in `LISTING.md`). Turning it on is a price edit in each
channel — no other work.
