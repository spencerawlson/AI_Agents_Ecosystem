# Community listening

HeyCatch's listening watches Reddit, X and Threads for conversations
and drafts replies. The ecosystem's version starts where the evidence
actually reaches, and says so where it does not.

```bash
python businesses/road2cissp/growth.py listen            # search + draft (needs LLM)
python businesses/road2cissp/growth.py listen --no-llm   # search only; threads recorded as found
python businesses/road2cissp/growth.py listen --limit 3
python businesses/road2cissp/growth.py status            # community thread counts
```

## Sources

| Source | Status | Why |
| --- | --- | --- |
| Reddit | **Live** | Public search JSON (`/r/<sub>/search.json`), read-only, no key, no account. |
| X | Unavailable | No public search without API credentials — search redirects to login. |
| Threads | Unavailable | No public search without API credentials. |

Every listen report lists all three with their status. X and Threads
are never faked; the `SOURCES` registry in
`core/community_listening.py` is where a bearer-token transport plugs
in later.

**Reddit needs a residential IP.** From a datacenter IP Reddit
answers with its "blocked due to a network policy" page — the same
class of block as Google Trends' 429s. Run `listen` from FedServ,
like the Trends job. From a blocked network the listener reports
`reddit: unavailable` with the reason and finds zero threads; it
never invents threads to fill the gap.

## How it works

1. **Search.** Each subreddit in the site profile (`r/cissp`,
   `r/cybersecurity`, `r/ITCareerQuestions`, `r/CompTIA`, `r/ccna`)
   is searched for the business's keywords
   (`LISTEN_KEYWORDS` in `businesses/road2cissp/growth.py`), newest
   first, past week only. At most 10 queries per run, interleaved so
   every subreddit is covered before any second keyword.
2. **Filter.** NSFW, locked, deleted-author and older-than-14-days
   threads are dropped, with the reason recorded.
3. **Score.** Deterministic, 0–100, no LLM — a question (+35), a
   stated problem (+25), a decision being made (+20), a keyword in
   the title (+15, body +8), a real discussion already (+5),
   promotional signals (−30). **≥ 50 is worth a reply, ≥ 75 is high
   intent.** Every score carries its reasons.
4. **Draft.** The creative agent (`community_replies`, cheap tier)
   writes one reply per thread worth answering, at most 5 per run.
   Every draft must pass the deterministic 90/10 gate
   (`validate_reply_draft`): answer first — at least 200 characters
   of genuine help — then the site mentioned at most once, only in
   the final sentence, disclosed as "I built", with no promo language
   and at most one link. Drafts that fail are dropped and reported,
   never posted.
5. **Nothing is posted, ever.** Drafts are files under
   `reports/community_drafts/<thread-id>.md` and creative assets of
   type `community`. The owner posts a reply by hand, then marks it
   done — the daily plan lists drafted replies as
   `reply:<thread-id>` items, above the due jobs, because a thread
   goes stale in days.

Found threads are stored in the growth state under
`community_threads` (`found` → `drafted` / `dropped` → `done`),
so a thread is never drafted twice. `status` prints the counts.
