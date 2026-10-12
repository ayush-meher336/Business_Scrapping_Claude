# leadforge — ScrapTrade lead research platform

Python pipeline for discovering, enriching and organising business leads. Design:
[architecture document](https://claude.ai/code/artifact/903bf892-91a7-45ae-9175-a64829d8330b).

**Status:** Phase 1 (workbook migration) complete; Phase 2 (geography and keywords) in progress.

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
