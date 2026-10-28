# QuickBooks Automation Dashboard

A local web dashboard that turns a live insurance-policy feed into customers,
jobs and invoices inside **QuickBooks Desktop** — reviewed on screen first, sent
in batches, and safe to run twice.

Built for an insurance agency back office that was re-keying thousands of
policies into QuickBooks by hand.

> **Stack:** Python 3.14 (standard library only, plus `pywin32`) · QuickBooks
> Desktop SDK over COM · vanilla JavaScript front end · SQLite · PyInstaller

---

## What it does

A Google Sheet holds the policies. The dashboard reads it every minute and shows
every row with what QuickBooks is missing for it. You tick the rows you want and
press send.

For each policy it will:

1. **Find or create the customer** — the insured corporation.
2. **Create a job under that customer** — one per policy, named from the policy.
3. **Create up to three invoices**, each to its own receivable account:

   | Invoice | Account | Amount from | Item |
   |---|---|---|---|
   | Premium | `Accounts Receivable:Premium` | Net due | Standard / Non-Standard, by carrier type |
   | Commission | `Accounts Receivable:Commission` | Commission | The carrier |
   | Fees | `Accounts Receivable:Handling Fees` | McKinney Fee | Fees |

Accounts, templates, items, terms and sales reps are all matched by name;
missing items, terms and reps are created on demand.

## Running it twice is safe

Two independent guards, because either alone can be wrong:

- **A local history database** records every row that finished, with the
  QuickBooks IDs it produced.
- **QuickBooks itself is asked** whether the customer, job and invoice number
  already exist before anything is created.

Premium and commission invoices share one policy number, so they are told apart
by the receivable account they post to rather than by reference alone. Each
company file gets its own history database — carrying one across companies would
make the app skip real work.

---

## The interesting problems

**qbXML demands exact element order.** The schema is a sequence, not a bag:
`CustomerRef`, `ClassRef`, `ARAccountRef`, `TemplateRef`, `TxnDate`,
`RefNumber`, `TermsRef`, `SalesRepRef`, `Memo`, then the lines. Two elements
swapped and the whole request is rejected. The SDK's own reference XML is the
only authority worth trusting here.

**Custom fields on transactions are unreachable.** Public data extensions
(`OwnerID 0`) cannot be assigned to an invoice at all, so "Transaction Type" and
"Source" are not custom fields — the invoice templates rename the built-in
**Terms** and **Rep** fields to those labels, and the app writes `TermsRef` and
`SalesRepRef`.

**An open Customer Centre can crash QuickBooks.** It holds the customer list
open, and writing to that list underneath it fails with *"in use by another
user"* — or takes QuickBooks down outright, reproducibly, part-way through a
batch. The app reads open window titles through `EnumWindows`, refuses to start
a batch while that window is open, and retries transient lock errors.

**QuickBooks serves exactly one company at a time.** `BeginSession("")` attaches
to whatever is open; naming a file instead asks QuickBooks to *start* it, which
fails when it is already running. The company picker tests a session before
saving a choice, so a company that could not be reached is refused up front
rather than failing at the next send.

**A company's name is not its file name.** QuickBooks shows the company name in
its title bar, which is set inside the file and need not match the file on disk.
Companies are therefore identified by path, never by name.

**Names must survive QuickBooks' own rules.** `:` is reserved for
`Customer:Job`, names cap at 41 characters, reference numbers at 11. Every name
is normalised before it is sent.

---

## Architecture

```
Google Sheet (published web app)
        │  fetched every 60s
        ▼
  websource.py ──► models.py ──► processor.py ──► qbxml.py
   (rows)          (one policy,    (what is        (builds the
                    what is         missing)        requests)
                    invalid)             │
                                         ▼
                                  quickbooks.py
                                   (COM session)
                                         │
                                         ▼
                              QuickBooks Desktop
```

| Module | Responsibility |
|---|---|
| `websource.py` | Reads the published sheet; fixed 21-column layout with a shape guard |
| `models.py` | One policy row: what it means, what makes it invalid, how names are built |
| `processor.py` | Decides what QuickBooks is missing, and runs a batch |
| `qbxml.py` | Builds qbXML requests and parses the replies |
| `quickbooks.py` | The COM session, plus a dry-run gateway with the same interface |
| `storage.py` | SQLite history — what was created, and every logged event |
| `runtime.py` | Application state: the loaded rows, the batch lock, company switching |
| `server.py` | A small `http.server` API; no web framework |
| `web/` | The dashboard — no build step, no framework |

There is a **dry-run gateway** implementing the same interface as the real one,
so the whole pipeline can be exercised, and the tests run, without QuickBooks
being installed at all.

---

## Try it without QuickBooks

The tests need nothing but Python:

```bash
python -m unittest discover -s tests -t .
```

To run the dashboard:

```bash
pip install -r requirements.txt
cp config.example.json config.json     # then set your own company and sheet link
python -m qb_agent --config config.json serve
```

It opens on <http://127.0.0.1:8770> and starts in `dry_run` mode — the full
pipeline runs and nothing reaches QuickBooks until `mode` and `allow_writes` are
both turned on, *and* the open company matches the configured one.

`config.example.json` is the template; the real `config.json` is not committed.

See [SETUP.md](SETUP.md) for installing on a machine that has QuickBooks.

---

## The dashboard

- Policies table with per-row state for customer, job and each invoice kind
- Filters by tab, status, invoice state, text and date; inline editing of rows
- Bulk send with a per-row progress tick and a hard cap on selection size
- **Pre-flight check** — every account, template and item matched by name in one
  pass, so a rename in QuickBooks surfaces before a batch instead of as 200
  failed rows
- **Company picker** — lists the company files on the machine, says which one
  QuickBooks has open and which one the app is aimed at, and warns when they
  disagree
- **Technical log** — every action with its steps, and the reason for each failure
- Light and dark themes
