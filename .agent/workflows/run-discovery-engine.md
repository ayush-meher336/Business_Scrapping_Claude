---
description: Set up (if needed) and run the leadforge discovery engine, then do the AI step continuously
---

Follow AGENTS.md. In short:

1. If `.venv` does not exist: `python -m venv .venv` then `.venv\Scripts\python -m pip install -e ".[dev]"`.
2. If `.env` does not exist: copy `.env.example` to `.env`; ask the user for `SHEETS_WEBAPP_URL` if it is empty.
3. Run `leadforge bootstrap` (safe to repeat), then `leadforge status`.
4. Start `leadforge autopilot` in a separate background terminal and leave it running.
5. Loop: `leadforge autopilot-status`; for each waiting batch in `data/ai/batches/`, write
   `data/ai/results/<batch_id>.result.json` following the batch's `instructions` (AGENTS.md section 4).
6. Watch for discovery `paused` (verification page) or worker `error` and handle per AGENTS.md section 5.
7. To stop: create `data/STOP`.
