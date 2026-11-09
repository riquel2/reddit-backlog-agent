# Reddit Data Access Request — answers to paste

Copy-paste pack for Reddit's Data Access Request form
(`support.reddithelp.com` → **Data Access Request** → role: **developer** →
"my Reddit app cannot run inside the Devvit ecosystem").

Answers are written in English on purpose: the reviewers read English even when
the form is auto-translated into Chinese. Keep the numbers accurate — they are
what the reviewer checks against your actual traffic.

---

## 1. Benefits / use for Reddit users

```
This tool never posts, votes, comments, messages, moderates, or interacts with
users in any way. It reads public posts and comments through the official Data
API in read-only mode (application-only OAuth, no user context). Reddit users
in the affected subreddits see no bots in their threads, no automated replies,
and no change to their experience.

What it produces is a private, aggregated summary of recurring complaints and
feature requests in a small set of product communities. The benefit to Reddit
users is indirect but concrete: it lets me learn what these communities have
already asked for publicly, so anything I build responds to expressed needs
rather than guesses.

A direct benefit is reduced load compared with unofficial scraping: one
registered client, sequential manual runs a few times a month, well under 100
queries per minute, and no crawling of deep comment trees.

No usernames are stored or displayed, no user profiles are built, and no
content is redistributed, published, or sold.
```

## 2. Detailed description of use on the Reddit platform (+ examples)

```
Scope: a small, fixed list of product and productivity subreddits
(r/SaaS, r/SideProject, r/productivity, r/Notion). No other communities.

Per run:
1. Authenticate with a script-type app in application-only (client_credentials)
   mode. No user context, no posting scopes.
2. For each subreddit, one of two listing calls:
   - /r/<sub>/top?t=year&limit=300   (recent top posts), or
   - /r/<sub>/search?q=<keyword>&restrict_sr=1&sort=top&t=year
     with 5-10 phrasing keywords such as "wish there was",
     "alternative to", "feature request", "I would pay for".
3. Drop stickied/mod posts and NSFW posts. Keep title, selftext, score, and the
   top 5-10 comments by score (top level only; "load more" branches are
   discarded without extra requests).
4. Cache to a local JSON file so the same post is never fetched twice.
5. Send the text to an LLM for grouping into themes, then write a local
   Markdown/Excel summary for my own reading.

Volume and frequency (hard limits I keep):
- 4 subreddits x up to 300 posts = at most ~1,200 posts per run
- at most ~12,000 comments per run (10 per post)
- 2-3 runs per month, run by hand, sequential, never concurrent
- total API traffic stays far below 100 queries per minute

Retention: the local cache and any raw text are deleted within 7 days of each
run. Only the aggregated report (no usernames, no raw comment dumps) is kept.

Worked example: /r/SaaS, search "wish there was", t=year, 300 posts. Output is
clustered into themes such as "bulk CSV import with column mapping", "self-serve
data export", "audit log access on lower tiers", each with a post count, a
demand score, and 2 verbatim public quotes with permalinks so I can re-read the
original thread. A sample of the output format, generated from synthetic test
data, is attached to this request.
```

## 3. What Devvit is missing

```
Devvit is not applicable to this use case, for structural reasons:

1. Installation model. Devvit apps are installed into a specific subreddit by
   that subreddit's moderators. My analysis targets third-party communities I
   do not moderate (r/SaaS, r/SideProject, r/productivity, r/Notion), so there
   is no subreddit in which this app could be installed.

2. Runtime and output. The tool is an offline Python batch job that produces
   local Markdown/Excel files on my own machine. Devvit runs serverless
   TypeScript with Redis as its only persistence layer and no filesystem, so it
   cannot emit local report files or host this pipeline.

3. Dependencies. The pipeline relies on Python libraries (PRAW, pandas,
   openpyxl) and on a user-configurable LLM endpoint. Devvit's fetch layer
   requires per-domain allowlist approval, and personal/custom endpoints are not
   approved.

4. Trigger model. Devvit apps are event-, menu-, or scheduler-driven inside a
   subreddit. This tool is invoked manually by me, on my own computer, a few
   times a month.

There is no Devvit app for this project; no app link exists.
```

## 4. Source code link

Public repository:

```
https://github.com/riquel2/reddit-backlog-agent
```

If asked to point at the data-access layer specifically:
`scraper.py` (`_iter_submissions`, `_extract_post`, `_top_comments`).

## 5. Subreddits

```
r/SaaS, r/SideProject, r/productivity, r/Notion
```

Read-only. Nothing is installed into these communities and nothing is posted to
them.

## 6. Username the bot runs under

Your own Reddit account name, without the `u/` prefix.

---

## Before you submit

- [ ] Only one submission for this use case. Duplicates are treated as abuse.
- [ ] The volume numbers match what the code actually does
      (`--limit`, `--comments`, run frequency).
- [ ] Attachment: `docs/samples/sample_report.md` — it shows the output format
      and proves no usernames appear.
- [ ] Do not write "I can't use Devvit because I don't know TypeScript". That is
      a skills gap, not a platform limitation, and it gets rejected.
- [ ] Do not describe the tool as "just scraping data for research" — that
      reframes it as a Data API consumer and loops the request back to the start.

## Reality check

New public Data API access requests closed on **2026-10-31**. This form is the
only remaining door, and a reply is not guaranteed. If it is declined:

1. Check `reddit.com/prefs/apps` for an older app — existing credentials still
   work until 2027.
2. Or bring your own data: write posts into `data/raw_data.json` (see README
   section 14) and run `start_cli.bat --from-cache`. Analysis, scoring, reports
   and Excel all work unchanged.
