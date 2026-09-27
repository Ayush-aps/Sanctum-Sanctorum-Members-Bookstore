# NOTES.md — Sanctum Sanctorum Bookstore

**Live URL:** https://sanctum-sanctorum-56pb.onrender.com/

**Repo:** https://github.com/Ayush-aps/Sanctum-Sanctorum-Members-Bookstore

## Signing in

Sign-in on the live site is by member ID only. Seed data is inserted on first boot
(`seed_if_empty`), and I've checked these IDs against the live/seeded database directly, so
they're confirmed, not guessed:

| ID | Name | Tier | Useful for testing |
|---|---|---|---|
| 1 | Wong Li | supreme | No loan limit, can access restricted books — good account for exercising the full feature set |
| 2 | Christine Palmer | master | Also has restricted-book access, but does have a loan cap |
| 3 | Jonathan Pangborn | adept | Mid-tier discount (5%), loan limit of 3 |
| 4 | Sara Lin | apprentice | Loan limit of 1, blocked from restricted books — good account for exercising 403s |

Two of the seeded books are `restricted: true` ("Darkhold" and "Principles of Celestial
Mechanics"), which is what makes tier-gating actually visible in the UI.


## What I finished

- **Books**: create (ISBN-13 checksum validation, duplicate-ISBN rejection with a race-safe
  `IntegrityError` fallback matching the pattern used for members), get by id, list (search by
  title/author, restricted filter, min/max price, sort by title/price asc/desc with id
  tiebreak, pagination with correct `total` computed before pagination), partial update via
  PATCH (isbn silently ignored, unknown fields ignored, unset fields left untouched).
- **Members**: create (name/email validation, case-insensitive duplicate email → 409, race-safe
  via `IntegrityError` fallback), get by id, list a member's orders, member stats (paid order
  count/spend, active/overdue loan counts, late fee totals — computed in one SQL round trip),
  list a member's loans with optional status filter.
- **Orders**: create (member/book existence checks, restricted-book tier gating, all-or-nothing
  stock reservation, tier + bulk quantity discount pricing with floor-division cents math), get
  by id, pay (pending → paid, idempotent via conditional UPDATE), cancel (pending → cancelled,
  restores reserved stock).
- **Loans**: create (the full ordered check list from SPEC.md — member/book existence, restricted
  access, overdue block, duplicate-book block, tier loan limits, stock), get by id, return
  (late fee calculation with the ceil-partial-day rule, capped at book price, stock restored),
  status computed at read time everywhere it's needed (not stored).
- **Reports**: top-selling books over paid orders only, correct tie-break sort.
- **Deployment**: Dockerized, deployed on Render, Postgres via Supabase in production while
  keeping the default SQLite path for local dev/tests. `db.py` rewrites `postgresql://` and
  `postgres://` URLs to `postgresql+psycopg://` (so SQLAlchemy picks the `psycopg` v3 driver I'm
  actually depending on, instead of falling back to a default that may not be installed, and so
  the older `postgres://` scheme some providers still hand out is recognized at all), leaves
  Postgres `connect_args` empty since anything provider-specific like `sslmode=require` travels
  in the connection string itself, and sets `pool_pre_ping=True` because a hosted DB can drop
  idle connections silently — worth having once you're not on a local SQLite file anymore. None
  of this touches the SQLite path, so `uv run pytest` still runs fully offline against the
  default setup, per the hard rule in INSTRUCTIONS.md §4.
- **A couple of bugs I found outside the TODO list, while testing:**
  - `app/services/members.py`'s `tier_at_least` used `>` instead of `>=` when comparing tier
    ranks. With strict `>`, a member whose tier exactly equals the required minimum (e.g. a
    `master` member checked against `minimum="master"` for restricted-book access) would
    incorrectly fail the check. Fixed to `>=` so "at least this tier" means what it says.
  - The frontend's "Return" button on the Loans tab wasn't clickable at all. The click delegator
    in `frontend/app.js`'s `initActions()` switches on `target.dataset.action`, and the
    `'loan-return'` case was simply missing from the switch — so clicks silently fell through to
    the `default: break` and did nothing. I found this by clicking through the deployed UI (there
    was no TODO pointing at the frontend, since none of the assignment's todos were in that
    folder), and added the missing case, wiring it to the existing `returnLoan(id, target)`
    function that was already defined elsewhere in the file.

### Optional extras (from ASSIGNMENT.md)

- ✅ **Handle concurrent orders for the last copy of a book safely** — done. Row-level locking
  (`with_for_update`) on members/books during loan and order creation, combined with an atomic
  conditional `UPDATE ... WHERE stock >= qty` (checked via `rowcount`) for the actual
  decrement/increment, so two simultaneous requests for the last copy of a book can't both
  succeed, and a failure partway through a multi-item order rolls back every stock change made
  so far in that order.
- ⚠️ **Add tests for edge cases I think are missing** — partial, not exhaustive. I added three
  targeted cases to the existing `tests/test_books.py` and `tests/test_orders.py` files (no
  existing test was changed or removed — ASSIGNMENT.md's ground rule is "do not modify anything
  in `tests/`", which I've read as "don't touch the existing acceptance criteria," not as
  forbidding the additions this same optional extra explicitly invites):
  - a parametrized case confirming that PATCHing any single book field with an explicit `null`
    (as opposed to just omitting it) returns 422 and leaves the book completely unchanged —
    this is what actually proves my `BookUpdate.reject_explicit_nulls` validator is doing
    something, rather than just existing;
  - a case confirming `GET /books?q=%` treats `%` as a literal character rather than a SQL LIKE
    wildcard (title search uses `icontains(..., autoescape=True)`, and this is the test that
    would catch it if that escaping ever regressed);
  - a case confirming that an order mixing a restricted book with a public one, placed by a
    member below the restricted-access tier, is rejected as a whole (403) with neither book's
    stock touched and no order row created — a different failure path than the 409
    insufficient-stock all-or-nothing case that presumably already has coverage.
  These aren't a systematic sweep of every edge case, just the three I ran into and thought were
  worth locking down. Full suite is 209/209 passing (`uv run pytest`), including these three.
- ❌ **`GET /members` with pagination** — not implemented.

## What I did not finish

Nothing required by SPEC.md that I'm aware of — see the optional-extras breakdown above for the
one thing (`GET /members` pagination) I left out due to lack of time.

## Architectural decisions & trade-offs

- **Business logic lives entirely in `services/`**, routers only parse/delegate/return, matching
  the requested layering. `app/services/members.py`'s tier-ordering helpers
  (`tier_at_least`, `ensure_can_access_restricted`) are reused by `orders.py` rather than
  duplicated — a small cross-service dependency I considered moving into a shared
  `policies.py`, but kept in `members.py` since tier semantics are conceptually a "member"
  concern.
- **Stock integrity via two layered mechanisms, not one**: `SELECT ... FOR UPDATE` row locking
  plus an atomic conditional `UPDATE` (checking `rowcount`) for every stock change. The
  conditional `UPDATE` alone is what actually prevents stock from going negative under a race —
  the row lock is there for a second reason: it also serializes against a concurrent
  `PATCH /books/{id}` changing `price_cents` while an order is mid-flight, so a price snapshot
  taken during order creation can't be invalidated by an in-progress edit. On SQLite (used in
  tests) the lock is effectively a no-op since SQLite doesn't do row-level locking; on Postgres
  in production it's real.
- **Consistent duplicate-handling across unique constraints**: both `create_book` (ISBN) and
  `create_member` (email) use the same two-step pattern — a pre-check `select` for the common
  case, plus a fallback `except IntegrityError` re-check for the genuine race between two
  concurrent identical-value creates. I originally only had this on `create_member` and caught
  the inconsistency on a later review pass (see AI usage below).
- **`BookUpdate` rejects explicit `null`s** for any field present in the PATCH body, via a
  `model_validator`. SPEC.md doesn't ask for this, but an explicit `null` for something like
  `stock` seemed more likely to be a client bug than an intentional "clear this field" — nothing
  in the domain is actually nullable there. Willing to be argued out of this; it's a judgment
  call, not something the spec dictated.
- **Late fee / order pricing use integer floor math throughout** (`subtotal * percent // 100`)
  rather than floats, to avoid rounding drift on money — matches SPEC.md's explicit
  floor-division rule for discounts. Late fees use `ceil` on partial days instead, per spec.
- **SQLite locally, Postgres (Supabase) in production** — see the deployment bullet above; the
  short version is that the URL rewrite, `connect_args`, and `pool_pre_ping` in `db.py` are all
  there specifically because of the Postgres move, and none of it affects the SQLite/test path.
  One thing I'd watch under real load: SQLAlchemy's default pool (`pool_size=5`,
  `max_overflow=10`) can open up to 15 connections, which could bump into Supabase's free-tier
  connection cap — not an issue at this scale, but I'd tune the pool size or switch to Supabase's
  pooler endpoint if that ever became a problem.

## Things in the spec I found unclear or questionable

- SPEC.md says mixed-case sort ordering is "unspecified" (SQLite vs Postgres disagree on
  collation) and that either behavior is accepted — worth double-checking the test suite
  genuinely doesn't assert a specific order here, since it's the one place local SQLite and
  production Postgres could visibly disagree.
- The "do not modify `tests/`" ground rule and the "add tests for edge cases" optional extra
  read as being in slight tension with each other — I've resolved it (see above) by only adding
  new test functions to existing files and never touching what was already there, but I'd have
  appreciated the spec being explicit that additions are fine.

## AI usage

I used **ChatGPT** as my main assistant for the backend — scaffolding service functions against
SPEC.md, debugging, and rubber-ducking design questions like the all-or-nothing order-creation
requirement and the tradeoffs between optimistic vs. pessimistic locking for the stock-reservation
race. I didn't treat its output as final: I went back through the ordered-check-list endpoints
(loans, orders) function by function against SPEC.md, since getting the *sequence* of 404/403/409
checks right mattered more than any individual check being correct on its own.

Two concrete places it got something wrong, or I had to redirect it:

- My first pass at `create_book` (ChatGPT-assisted) only did a pre-check `select` for ISBN
  uniqueness, while `create_member` (drafted separately) had an additional `IntegrityError`
  fallback closing the race window between two concurrent identical-value creates. The AI hadn't
  flagged that the two functions, solving the same class of problem, had diverged — I caught
  that myself comparing them side by side later, then had it regenerate `create_book`'s
  uniqueness check to match, and verified the two against each other line by line.
- ChatGPT's first draft of `create_order`, `pay_order`, and `cancel_order` wrapped the *entire*
  function body — including the read-only 404/403 checks at the top, before anything is
  mutated — in a blanket `try: ... except Exception: db.rollback(); raise`. It's harmless (a
  rollback on a session with no pending changes is a no-op), but it's sloppy: it routes every
  simple validation failure through exception-based control flow for no reason, and makes it
  harder to tell, just by reading the function, which part of it actually needs transactional
  safety. I narrowed the `try` block in all three functions down to only the section that
  performs an actual mutation (the stock reservation and order creation in `create_order`; the
  post-status-change load/commit in `pay_order` and `cancel_order`), so a rollback only ever
  fires when something real was changed and then failed.

For the frontend, I used Gemini as a debugging partner on the "Return" button on the Loans tab,
which produced no visible update and no network activity when clicked. First check was the
browser dev tools — Network and Console tabs while clicking the button. No request ever went out
to `/loans/{id}/return`, and there were no JS errors in the console either, which ruled out both
a backend/API problem and a JS runtime error; whatever was wrong was happening purely at the
event-binding level, before any request was even attempted. Next I checked the markup itself in
`renderLoans()` — the button was rendering correctly with the right `data-action`/`data-id`
attributes, so it wasn't a templating bug. That pointed at the event handling, so I traced how
clicks actually get wired up: the app uses a single delegated `click` listener on `document`,
routed through `initActions()`, which reads `e.target.dataset.action` and dispatches on a
`switch` statement. Reading through that switch is where the actual bug was — it had cases for
things like `'create-book'`, `'create-member'`, `'place-order'`, and others, but nothing for
`'loan-return'`. With no `default` case logging anything, a click on Return just fell through the
whole switch and did nothing, silently. Fix was adding the missing
`case 'loan-return': returnLoan(id, target); break;`, wiring it to the `returnLoan` function that
was already defined and used elsewhere in the file — the handler existed, it just wasn't
reachable from a click.

I can walk through and defend every check, query, and validator in this codebase — including why
the row-locking is redundant-but-harmless under SQLite and load-bearing under Postgres, why late
fees use `ceil` while discounts use floor, why the stock reservation is done via a conditional
`UPDATE` rather than trusting the earlier `SELECT`, and why the three edge-case tests I added
check what they check.