# leadforge — ScrapTrade lead research platform

Python pipeline for discovering, enriching and organising business leads. Design:
[architecture document](https://claude.ai/code/artifact/903bf892-91a7-45ae-9175-a64829d8330b).

**Status:** Phases 1-6 built (migration, geography/keywords, browser discovery + dedup, website enrichment, agent-run AI, Google Sheets sync). No third-party data APIs; no model API calls in the code.

An AI agent operating this project should read [AGENTS.md](AGENTS.md) first.

## Setup on a new machine

Needs Python 3.12+ and git. Works on Windows, macOS and Linux.

```bash
git clone https://github.com/ayush-meher336/Business_Scrapping_Claude.git
cd Business_Scrapping_Claude
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
```

The lead workbook and the database are **not** in the repository (they hold business contact
details). Copy `Scrap_Outreach_Lead_Tracker.xlsx` into the project folder, then rebuild:

```bash
leadforge migrate        # workbook -> data/lead_research.db, verified
leadforge geo-import     # downloads GeoNames (about 14 MB) into data/cache/
leadforge geocode
leadforge keywords
leadforge build-zones --tier 1 --priority A
```

Activate the environment first (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere) or call `.venv/Scripts/leadforge` directly.

## Commands

```bash
leadforge migrate            # import the workbook into data/lead_research.db and verify it
leadforge migrate --force    # rebuild the database from the workbook
leadforge verify             # re-check the database against the workbook
leadforge status             # counts
leadforge check-niche scrapyards
pytest
```

### Phase 2: geography and keywords

```bash
leadforge geo-import                         # download GeoNames (cached) and load places
leadforge geocode                            # coordinates for cities and zones; link leads to zones
leadforge build-zones --tier 1 --dry-run     # preview new zones; drop --dry-run to write
leadforge keywords                           # sync keyword library + yields from the research log
leadforge plan --country Australia --source google_maps_browser --source web_search_browser
leadforge coverage --country Australia
```

### Phase 3: discovery (SeleniumBase, no APIs)

```bash
leadforge scan-industrial --tier 1 --limit 10          # Google Maps: industrial estates per city (zone evidence)
leadforge build-zones --tier 1                          # turn that evidence into zones
leadforge plan --country Australia --city Sydney        # queue Maps searches (zone x keyword)
leadforge discover --jobs 30 --country Australia        # run them: 2 browsers, results resolved as they arrive
leadforge dedup-report                                  # resolution counts + CSV sample for a manual duplicate check
```

Google Maps is read from the results list in one pass (listings are not opened one by one).
Pacing is 4-8 s per search per browser. If Google shows a verification page the run stops with
exit code 2; nothing tries to get past it. Check it in a normal browser before running again.

### Phase 4: website enrichment (plain HTTP; browser only for JavaScript-only sites)

```bash
leadforge enrich --scope new                    # newly discovered leads
leadforge enrich --scope missing-email --limit 2000
leadforge enrich-bench --limit 200              # email recall vs. leads whose email is already known (no writes)
```

Per site: homepage + up to 4 relevant pages (contact, about, materials, locations), robots.txt
respected, 20 s budget. Collects emails (incl. Cloudflare-protected and "[at]/[dot]" forms), phones,
JSON-LD address, LinkedIn, contact page, opt-out notices, website status and cleaned page text
(for the AI step). Filled fields are never overwritten. `Email Verified = Valid` means the address
is well-formed and its domain has mail servers (MX); there is no mailbox-level check.

### Phase 5: AI qualification, done by the operating agent

```bash
leadforge ai-export --limit 200   # batches of 20 with labelled evidence + instructions -> data/ai/batches/
#   the agent writes data/ai/results/<batch_id>.result.json (see AGENTS.md)
leadforge ai-import               # validated import; doubtful results go to human review
leadforge ai-status
```

The importer only publishes a description when every material, number and scale claim in it
appears in that business's evidence, all cited sources exist, values come from the Lists tab and
no marketing phrasing is used. Results are matched by id, never by position.

### Phase 6: Google Sheets sync (the shared record between machines)

Setup (once per spreadsheet), using the Apps Script web app in [apps_script/Code.gs](apps_script/Code.gs):

1. Open the spreadsheet, **Extensions > Apps Script**, replace the editor contents with `Code.gs`, save.
2. **Deploy > New deployment > Web app**: Execute as **Me**, Who has access **Anyone**. Authorise.
3. Copy `.env.example` to `.env` on each machine and set `SHEETS_WEBAPP_URL` to the `/exec` URL.

The web app has no password: anyone with the URL can read and write the sheet, so keep it private.
After editing `Code.gs`, use **Deploy > Manage deployments > Edit > New version** so the URL stays
the same. (Alternative: a service account via `GOOGLE_SERVICE_ACCOUNT_JSON` + `GOOGLE_SHEET_ID`.)

```bash
leadforge sync                    # Leads, Search Zones, Cities (two-way) + Lead_Sources (append)
leadforge sync --tab Leads
```

Each machine keeps its own SQLite working copy; the sheet is shared. Rows are matched by the hidden
`Lead Key` column, never by row number. Sales columns (priority, outreach status, decision maker,
review notes) always keep the sheet's value; research columns are written by the database unless a
person edited the cell since the last sync, in which case the edit wins and is pulled in (logged in
`sync_conflict`); notes from both sides are combined. Rows added by other machines are pulled in,
rows typed into the sheet by hand get a key, and the original workbook's rows are adopted by Lead ID
and name instead of being duplicated. Do not delete or edit the `Lead Key` column.

Excluded countries (sanctions / instability, from the workbook footnote) are listed in
`config/geo.yaml`; `build-zones` and `plan` refuse them.

## What `migrate` does

1. Stores every non-blank row of all 10 tabs verbatim in `legacy_row` (the lossless layer).
2. Records each column's old formula in `legacy_formula`.
3. Builds structured tables: `business`, `contact`, `source_record`, `country`, `geo_place`,
   `zone`, `research_log`, `enum_value`, `city_size`, `source`, `compliance_rule`.
4. Freezes the old `Lead ID` (`=ROW()-1`) as a stored number and gives each lead a permanent
   `lead_key` (ULID).
5. Re-reads the workbook and checks every row and all 39 lead fields, writing a report to
   `data/exports/`.

The workbook itself is never modified.

## Layout

- `config/app.yaml` paths, concurrency, AI model
- `config/niches/scrapyards.yaml` categories, keywords, qualification rules, sheet columns + field ownership
- `src/leadforge/storage/` schema and connection
- `src/leadforge/migration/` workbook reader, importer, verifier
- `src/leadforge/niches/` niche config model and validation
