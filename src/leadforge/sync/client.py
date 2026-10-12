"""Spreadsheet access behind a small interface: the real Google Sheets client and an in-memory fake."""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Protocol


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetClient(Protocol):
    def read(self, tab: str) -> list[list[str]]: ...
    def write_cells(self, tab: str, updates: list[tuple[int, int, list[list]]]) -> None: ...
    def append(self, tab: str, rows: list[list]) -> None: ...
    def ensure_tab(self, tab: str, cols: int) -> None: ...


class MemorySheets:
    """Test double that behaves like a spreadsheet (lists of rows of strings)."""

    def __init__(self):
        self.tabs: dict[str, list[list[str]]] = {}
        self.calls = 0

    def ensure_tab(self, tab, cols):
        self.tabs.setdefault(tab, [])

    def read(self, tab):
        self.calls += 1
        return [list(r) for r in self.tabs.get(tab, [])]

    def write_cells(self, tab, updates):
        self.calls += 1
        grid = self.tabs.setdefault(tab, [])
        for row, col, block in updates:                      # 1-based row/col of the block's top-left
            for i, values in enumerate(block):
                r = row - 1 + i
                while len(grid) <= r:
                    grid.append([])
                line = grid[r]
                for j, v in enumerate(values):
                    c = col - 1 + j
                    while len(line) <= c:
                        line.append("")
                    line[c] = "" if v is None else str(v)

    def append(self, tab, rows):
        self.calls += 1
        self.tabs.setdefault(tab, []).extend([["" if v is None else str(v) for v in r] for r in rows])


class GoogleSheets:
    """gspread over a service account. Writes are batched; 429/5xx are retried with backoff."""

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    MAX_CELLS_PER_CALL = 40000

    def __init__(self, credentials_path: str | Path, spreadsheet_id: str):
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(str(credentials_path), scopes=self.SCOPES)
        self.gc = gspread.authorize(creds)
        self.book = self.gc.open_by_key(spreadsheet_id)
        self._ws: dict = {}

    @classmethod
    def from_env(cls, root: Path) -> "GoogleSheets":
        env = load_env(root / ".env")
        path, sheet = env.get("GOOGLE_SERVICE_ACCOUNT_JSON"), env.get("GOOGLE_SHEET_ID")
        if not path or not sheet:
            raise RuntimeError("Set SHEETS_WEBAPP_URL (Apps Script web app) or GOOGLE_SERVICE_ACCOUNT_JSON + "
                               "GOOGLE_SHEET_ID in .env (see .env.example)")
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            raise RuntimeError(f"Service-account file not found: {p}")
        return cls(p, sheet)

    def _retry(self, fn, *a, **kw):
        import gspread
        for attempt in range(6):
            try:
                return fn(*a, **kw)
            except gspread.exceptions.APIError as exc:
                code = getattr(exc.response, "status_code", 0)
                if code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise
                time.sleep(min(64, 2 ** attempt) + random.random())

    def ws(self, tab):
        if tab not in self._ws:
            self._ws[tab] = self._retry(self.book.worksheet, tab)
        return self._ws[tab]

    def ensure_tab(self, tab, cols):
        import gspread
        try:
            self.ws(tab)
        except gspread.exceptions.WorksheetNotFound:
            self._ws[tab] = self._retry(self.book.add_worksheet, title=tab, rows=1000, cols=max(cols, 26))

    def read(self, tab):
        return self._retry(self.ws(tab).get_all_values)

    def write_cells(self, tab, updates):
        batch, cells = [], 0
        for row, col, block in updates:
            width = max((len(r) for r in block), default=1)
            rng = f"{col_letter(col)}{row}:{col_letter(col + width - 1)}{row + len(block) - 1}"
            batch.append({"range": rng, "values": [["" if v is None else v for v in r] for r in block]})
            cells += width * len(block)
            if cells >= self.MAX_CELLS_PER_CALL:
                self._retry(self.ws(tab).batch_update, batch, value_input_option="RAW")
                batch, cells = [], 0
        if batch:
            self._retry(self.ws(tab).batch_update, batch, value_input_option="RAW")

    def append(self, tab, rows):
        for i in range(0, len(rows), 500):
            chunk = [["" if v is None else v for v in r] for r in rows[i:i + 500]]
            self._retry(self.ws(tab).append_rows, chunk, value_input_option="RAW", insert_data_option="INSERT_ROWS")


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE lines); real environment variables take precedence."""
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID", "SHEETS_WEBAPP_URL"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


class AppsScriptSheets:
    """The spreadsheet through the Apps Script web app in apps_script/Code.gs (runs as its owner)."""

    ROWS_PER_CALL = 2000          # keeps each call well inside Apps Script's time and size limits

    def __init__(self, url: str, timeout: float = 300.0, transport=None):
        import httpx
        self.url = url
        # Apps Script answers a POST with a redirect to the result; follow it.
        self.http = httpx.Client(follow_redirects=True, timeout=timeout, transport=transport)

    def _call(self, payload: dict) -> dict:
        import httpx
        for attempt in range(5):
            try:
                r = self.http.post(self.url, json=payload)
            except httpx.TransportError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt + random.random())
                continue
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError("The web app did not return JSON. Check the deployment: Execute as Me, "
                                   "access Anyone, and that SHEETS_WEBAPP_URL ends in /exec.") from None
            if data.get("status") != "success":
                raise RuntimeError(f"Apps Script error: {data.get('message')}")
            return data
        raise RuntimeError("Apps Script did not answer")

    def ensure_tab(self, tab, cols):
        self._call({"action": "ensure_tab", "tab": tab, "cols": cols})

    def read(self, tab):
        return self._call({"action": "read", "tab": tab}).get("values", [])

    def write_cells(self, tab, updates):
        batch, rows = [], 0
        for row, col, block in updates:
            batch.append([row, col, [["" if v is None else str(v) for v in r] for r in block]])
            rows += len(block)
            if rows >= self.ROWS_PER_CALL:
                self._call({"action": "write_cells", "tab": tab, "updates": batch})
                batch, rows = [], 0
        if batch:
            self._call({"action": "write_cells", "tab": tab, "updates": batch})

    def append(self, tab, rows):
        for i in range(0, len(rows), self.ROWS_PER_CALL):
            chunk = [["" if v is None else str(v) for v in r] for r in rows[i:i + self.ROWS_PER_CALL]]
            self._call({"action": "append", "tab": tab, "rows": chunk})


def client_from_env(root: Path):
    """Apps Script web app if SHEETS_WEBAPP_URL is set, otherwise a service account."""
    env = load_env(root / ".env")
    if env.get("SHEETS_WEBAPP_URL"):
        return AppsScriptSheets(env["SHEETS_WEBAPP_URL"])
    return GoogleSheets.from_env(root)
