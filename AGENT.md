# Operating guide for the AI agent

You operate `leadforge`. Python does the collecting, parsing, matching and saving; you do the
reading and judging in **Step 5**. Run commands from the project folder with the virtual
environment active (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` elsewhere).

## Rules that always apply

- No third-party data APIs. Google Maps and web search run through SeleniumBase (built in).
- If a command stops with **exit code 2** ("Google asked for verification"), stop and tell the
  user. Never try to get past a CAPTCHA or verification page.
- Never send emails. Outreach is out of scope.
- Do not edit the database or the workbook by hand; use the commands.

## The research cycle

Start every session with `leadforge sync` so rows other machines added are pulled in first, and
end it with `leadforge sync` so your results reach the shared sheet.

```bash
leadforge status                                   # what the database holds
leadforge scan-industrial --priority A --limit 10  # 1. industrial areas per city (Maps)
leadforge build-zones --priority A                 # 2. zones from that evidence
leadforge plan --country "<Country>"               # 3. queue Maps searches
leadforge discover --jobs 30 --country "<Country>" # 4. run them; results are de-duplicated
leadforge enrich --scope new                       # 5a. crawl the new leads' websites
leadforge verify-emails                            # 5b. MX check (Valid = domain accepts mail)
leadforge ai-export --limit 200                    # 5c. evidence batches for you  -> data/ai/batches/
#   ... you write data/ai/results/<batch_id>.result.json for each batch (below) ...
leadforge ai-import                                # 5d. validated import
leadforge sync                                     # 6. share results through the Google Sheet
leadforge coverage --country "<Country>"           # progress by city
```

## Step 5: qualifying businesses (your part)

1. `leadforge ai-status` lists the batches waiting for you.
2. Open `data/ai/batches/<batch_id>.json`. It contains:
   - `instructions`: follow them exactly.
   - `allowed`: the only permitted `industry_type` / `material_focus` values.
   - `output_schema`: the shape of each result object.
   - `records`: up to 20 businesses, each with an `id` and labelled `evidence` (`S0` = Google
     Maps listing, `S1…` = website pages, `N1` = earlier research notes).
3. Write **one JSON array** to `data/ai/results/<batch_id>.result.json` with exactly one object per
   record, copying each `id` unchanged. Example object:

```json
{"id": "01JA...", "relevant": "yes", "industry_type": "Scrap Yard / Metal Recycler",
 "material_focus": ["Non-ferrous (copper, brass, aluminium)", "Batteries"],
 "description": "Buys and processes copper, brass and aluminium scrap [S1] and car batteries [S2] at its Wetherill Park yard [S0], with free same-day pickup [S1].",
 "personalisation_hook": "Offers free same-day pickup of copper cable [S1].",
 "evidence_ids": ["S0", "S1", "S2"], "confidence": "high", "concerns": []}
```

4. Run `leadforge ai-import`. It rejects or sends to human review any result that cites a source it
   was not given, uses a value outside `allowed`, states a material / number / size that is not in
   the evidence, or uses marketing phrases. Read its summary; fix and re-export only if it reports
   malformed files.

Use only the evidence in the batch. If the evidence is thin, say `"relevant": "unsure"`,
`"confidence": "low"` and explain in `concerns`; a human will review it. Missing beats invented.
