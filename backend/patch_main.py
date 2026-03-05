#!/usr/bin/env python3
"""
patch_main.py
-------------
Removes Pick 3 code from main.py and wires in pick3_reports.py router.

Usage (from your backend directory):
    python patch_main.py

Backs up main.py → main.py.bak before modifying.
"""

import re
import shutil
from pathlib import Path

MAIN = Path("main.py")

if not MAIN.exists():
    print("ERROR: main.py not found. Run this from your backend directory.")
    exit(1)

# ── Backup ──────────────────────────────────────────────────────────────────
shutil.copy(MAIN, "main.py.bak")
print("Backup written → main.py.bak")

src = MAIN.read_text(encoding="utf-8")
original_len = len(src.splitlines())

# ── 1. Remove Pick3ReportRequest model (lines ~617-621) ────────────────────
src = re.sub(
    r"\nclass Pick3ReportRequest\(BaseModel\):.*?\n\n",
    "\n",
    src,
    count=1,
    flags=re.DOTALL,
)

# ── 2. Remove all Pick 3 endpoints + helpers + constants ───────────────────
#    Starts at the first @app.post("/api/reports/pick3") decorator
#    Ends just before "# ------------------\n# Menus Sync API"
src = re.sub(
    r"\n@app\.post\(\"/api/reports/pick3\"\).*?(?=\n# --{18}\n# Menus Sync API)",
    "",
    src,
    count=1,
    flags=re.DOTALL,
)

# ── 3. Add router import near the top (after load_dotenv()) ────────────────
if "from pick3_reports import router as pick3_router" not in src:
    src = src.replace(
        "load_dotenv()\n",
        "load_dotenv()\n\nfrom pick3_reports import router as pick3_router\n",
        1,
    )

# ── 4. Register router on the app instance ─────────────────────────────────
#    Insert right after `app = FastAPI(...)` line
if "app.include_router(pick3_router)" not in src:
    src = re.sub(
        r"(app = FastAPI\([^)]*\))",
        r"\1\napp.include_router(pick3_router)",
        src,
        count=1,
    )

MAIN.write_text(src, encoding="utf-8")
new_len = len(src.splitlines())
print(f"Done. main.py: {original_len} → {new_len} lines ({original_len - new_len} removed).")
print("Pick 3 router registered from pick3_reports.py.")
