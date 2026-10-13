# AGENTS.md: operating guide for AI agents

You are operating **leadforge**, ScrapTrade's business discovery and lead research engine. When the
user asks you to "run the discovery engine" (or similar), follow this file from top to bottom.
Everything you need is here; you do not need earlier conversations.

**Division of labour.** Python does the collecting, parsing, matching, saving and syncing, all
concurrently in one long-running process (`leadforge autopilot`). **You** do two things: keep that
process healthy, and do the AI work (reading website evidence and writing business descriptions)
that the code deliberately does not do. There are no model API calls in the code; you are the model.

---

## 1. Hard rules

1. **No third-party data APIs.** Google Maps and web search run through SeleniumBase (a real Chrome
   browser), company websites over plain HTTP. Do not add Google Places, search APIs, Overpass, etc.
2. **Never bypass a CAPTCHA or "verify you are human" page.** The code pauses itself when Google shows
   one (30 minutes, then retries). If it keeps happening, tell the user; do not try to get around it.
3. **Never send emails or contact businesses.** This system prepares leads only.
4. **Never invent facts.** In the AI step, use only the evidence given. Missing beats invented.
5. **Do not edit the database, the Google Sheet's `Lead Key` column, or `seed/` by hand.** Use the commands.
6. **Do not commit `.env`, `data/`, or `*.xlsx`.** They are gitignored on purpose (business contact data,
   the sheet's web-app URL).

---

## 2. First run on this machine (setup)

Run from the repository root. Windows commands shown; on macOS/Linux use `.venv/bin/` instead of
`.venv\Scripts\` and `source .venv/bin/activate`.

```bash
python --version                       # needs 3.12+; Google Chrome must be installed
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\activate
```

**The `.env` file.** If `.env` does not exist, copy `.env.example` to `.env`. It needs
`SHEETS_WEBAPP_URL=<the Google Apps Script web app URL ending in /exec>`. If you do not have that
URL, **ask the user for it** (it is not in the repository on purpose). Without it the engine still
runs, but results stay on this machine until it is set.

**The database.**

```bash
leadforge bootstrap      # builds data/lead_research.db: workbook if present, else seed/ + the Google Sheet
leadforge status         # sanity check: leads, countries, cities, zones
```

`bootstrap` is safe to re-run; it does nothing if the database already exists. On a fresh machine it
loads reference data from `seed/reference.json.gz`, downloads GeoNames (about 14 MB), builds the
keyword library and pulls every lead from the shared Google Sheet.

Optional check: `python -m pytest -q` (all tests should pass; they use no network).

---

## 3. Run the engine (long-running, concurrent)

Start it in a **separate, persistent terminal / background process** so it keeps running while you work:

```bash
leadforge autopilot                                  # all countries, by priority tier
leadforge autopilot --country Australia --country India   # or limit the scope
```

It runs five workers at the same time, each independently (one failing never stops the others):

| Worker | What it does, continuously |
| --- | --- |
| discovery | Keeps ~40 Google Maps searches queued: plans open zones; when a country runs out, scans the next cities for industrial areas and builds zones; moves through priority A, then B, then C cities. Stops searching zones/cities that stopped yielding new businesses (saturation). Pauses 30 min on a verification page. |
| enrichment | Crawls the websites of newly found businesses (emails, phones, contact page, opt-out notices, page text), checks email domains (MX), loads JavaScript-only sites in a browser. |
| ai_export | Keeps 3 evidence batches waiting for you in `data/ai/batches/`. |
| ai_import | Every 30 s, validates and imports any result file you write to `data/ai/results/`. |
| sync | Every 15 min, two-way sync with the shared Google Sheet. |

All state is in `data/lead_research.db`. Stopping and restarting resumes where it left off.

- **Status:** `leadforge autopilot-status` (or read `data/autopilot_status.json`), log in `data/autopilot.log`.
- **Stop:** press Ctrl+C in its terminal, or create an empty file `data/STOP`. Current steps finish first.
- **Restart** after a crash, reboot or code update: just run `leadforge autopilot` again.

---

## 4. Your ongoing job: the AI step

While autopilot runs, repeat this loop for as long as the user wants the engine running:

1. Run `leadforge autopilot-status` (it lists "AI batches waiting for the agent"), or list
   `data/ai/batches/*.json` that have no matching `data/ai/results/<same name>.result.json`.
2. Open one batch file. It is self-contained:
   - `instructions`: the rules for this batch. **Follow them exactly.**
   - `allowed`: the only permitted `industry_type` and `material_focus` values.
   - `output_schema`: the JSON shape of one result.
   - `records`: up to 20 businesses. Each has an `id` and labelled `evidence`: `S0` = Google Maps
     listing, `S1`, `S2`, … = pages of the business's own website, `N1` = earlier research notes.
3. For every record, write one result object (example below). Copy `id` unchanged.
4. Save **one JSON array** with all results to `data/ai/results/<batch_id>.result.json`
   (the exact path is in the batch's `results_file`). Write it in one go (write to a temporary name
   and rename, or write the whole file at once) so a half-written file is never imported.
5. Within 30 seconds autopilot imports it. Check `leadforge ai-status` or the log: results that fail a
   check go to human review automatically; you do not need to redo them.
6. Go back to step 1. If no batches are waiting, check again in a few minutes.

Example result object:

```json
{"id": "01JA...", "relevant": "yes", "industry_type": "Scrap Yard / Metal Recycler",
 "material_focus": ["Non-ferrous (copper, brass, aluminium)", "Batteries"],
 "description": "Buys copper, brass and aluminium scrap [S1] and car batteries [S2] at its Wetherill Park yard [S0], with free same-day pickup [S1].",
 "personalisation_hook": "Offers free same-day pickup of copper cable [S1].",
 "evidence_ids": ["S0", "S1", "S2"], "confidence": "high", "concerns": []}
```

How to judge well:

- `relevant`: "yes" if the business buys, processes, trades, exports or imports scrap or recyclable
  materials (or is a large scrap generator); "no" if it clearly is not (new-metal seller, rubbish removal
  without recycling, craft shop); "unsure" when the evidence is thin, a bot-check page, or a parked domain.
- `description` (≤ 60 words): what they do, materials/services, customer types, location. Put a source
  tag after each claim, e.g. `[S1]`. Only cite tags that exist in that record.
- `personalisation_hook` (≤ 25 words): one specific, verifiable detail for a sales email, or `null`.
- Never use marketing words ("leading", "premier", "one-stop shop", "best"). Never state sizes, tonnages,
  years, staff numbers or customer names unless the evidence says so word for word.
- `confidence`: high / medium / low. Use `concerns` for contradictions, possibly wrong website, closure.

What the importer checks (results that fail go to human review; a wrong `allowed` value is rejected):
id belongs to the batch; values are from `allowed`; every cited tag exists; every material, number and
scale claim in the description and hook appears in the evidence; no marketing phrasing.

---

## 5. Monitoring and what to do when…

| Situation | What to do |
| --- | --- |
| `autopilot-status` shows discovery `paused` with "verification page" | Normal; it resumes by itself after 30 min. If it happens repeatedly, tell the user (they may need to open Google Maps once in a normal Chrome window). Never try to bypass it. |
| A worker shows `error` | It retries automatically (backoff up to 15 min). Read `data/autopilot.log` for the cause. Report persistent errors to the user. |
| "Sheet sync off" at start | `.env` has no `SHEETS_WEBAPP_URL`. Ask the user for it, add it, restart autopilot. |
| Sync reports conflicts | Normal: a person edited a cell in the sheet; their edit won. Nothing to do. |
| Discovery says "no cities left in scope" | Everything in scope is covered. Tell the user; offer to widen `--country` or lower priorities. |
| The machine will sleep | Long runs need the computer awake (disable sleep while running). |
| Code updated on GitHub | Stop autopilot (`data/STOP`), `git pull`, `pip install -e ".[dev]"`, start autopilot again. |

---

## 6. Useful single commands (for checks or manual runs)

```bash
leadforge status | autopilot-status | ai-status | coverage --country Australia
leadforge sync                         # immediate sheet sync
leadforge dedup-report                 # duplicate check + CSV sample
leadforge bench --target 100 --city Sydney   # end-to-end speed benchmark
leadforge discover --jobs 30 --country Australia   # one discovery batch (autopilot does this for you)
leadforge enrich --scope new           # one enrichment batch
leadforge ai-export / ai-import        # one AI round by hand
leadforge verify                       # (machine that has the workbook) database vs workbook check
```

---

## 7. Background knowledge

- **What a lead is:** one business at one physical location, identified by a permanent `lead_key`
  (shown in the sheet as the last column, `Lead Key`). `Lead ID` is the human-friendly number.
- **Sources of truth:** each machine has its own SQLite working copy; the **Google Sheet is the shared
  record**. Sync is two-way and per cell: sales columns (Priority, Outreach Status, Decision Maker…,
  Review Notes, Collected By) always keep the sheet's value; research columns are updated by the engine
  unless a person edited the cell since the last sync; notes from both sides are combined.
- **OK to Email?** is computed: an email exists, no opt-out notice on the site, `Email Verified = Valid`
  (meaning the domain accepts mail: an MX check, not a mailbox check), not bounced/unsubscribed, not on the
  suppression list, country not excluded. The reason for each "No" is in `OK to Email reason`.
- **Excluded countries** (sanctions/instability) are listed in `config/geo.yaml`; the engine refuses them.
- **Deduplication:** results are matched to existing leads by Google place id, website + location,
  address + name/phone/website, or phone + similar name. A similar name alone never merges; it goes to
  review (`Duplicate?` column).
- **Configuration:** `config/app.yaml` (concurrency, pacing, saturation thresholds),
  `config/niches/scrapyards.yaml` (categories, keywords, relevance rules, sheet columns),
  `config/geo.yaml` (zone building, excluded countries).
- **Design document:** see the README for the architecture link and the phase-by-phase notes.
