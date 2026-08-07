"""
Diagnostic: can the existing Drive files be matched back to invoice records?

Read-only. Touches nothing in Drive or Mongo.

The backend used to discard pdf_filename, so historic invoices have no stored
link to their Drive file. This script prints what IS available on both sides and
tests whether the invoice number appears in the filename, which is the most
likely remaining join key.

    python inspect_match_keys.py
"""

import os
import re
from collections import Counter

import requests
from dotenv import load_dotenv

load_dotenv()

from app.oauth import get_drive_service          # noqa: E402
from app.drive_folders import FOLDER_MIME        # noqa: E402

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")


def norm(s):
    """Lowercase alphanumerics only - so 'INV-001' and 'inv001' compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def main():
    invoices = requests.get(f"{BACKEND_URL}/verification/invoices/all", timeout=60).json()
    print(f"Invoices: {len(invoices)}")

    # --- what fields do invoice records actually carry? ---
    top_keys, data_keys = Counter(), Counter()
    for inv in invoices:
        top_keys.update(inv.keys())
        data_keys.update((inv.get("invoice_data") or {}).keys())

    print("\n--- top-level fields (count of invoices having them) ---")
    for k, c in top_keys.most_common(20):
        print(f"  {k:24} {c}")

    print("\n--- invoice_data fields ---")
    for k, c in data_keys.most_common(25):
        print(f"  {k:24} {c}")

    # --- sample record ---
    if invoices:
        inv = invoices[0]
        d = inv.get("invoice_data") or {}
        print("\n--- sample invoice ---")
        for k in ("_id", "vendor_name", "pdf_url", "pdf_filename", "drive_file_id"):
            print(f"  {k:16} {inv.get(k)!r}")
        for k in ("invoice_number", "vendor_gstin", "invoice_date"):
            print(f"  data.{k:11} {d.get(k)!r}")

    # --- Drive side ---
    drive = get_drive_service()
    files, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{ROOT_FOLDER_ID}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false",
            fields="nextPageToken, files(id, name)", pageSize=200, pageToken=token,
        ).execute()
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break

    print(f"\nDrive files in root: {len(files)}")
    print("--- sample filenames ---")
    for f in files[:12]:
        print(f"  {f['name']}")

    # --- can invoice_number be found inside the filename? ---
    numbers = []
    for inv in invoices:
        d = inv.get("invoice_data") or {}
        n = d.get("invoice_number")
        if n and len(norm(n)) >= 4:      # too-short numbers match everything
            numbers.append((norm(n), inv))

    hits, ambiguous = 0, 0
    examples = []
    for f in files:
        fn = norm(f["name"])
        m = [inv for n, inv in numbers if n in fn]
        if len(m) == 1:
            hits += 1
            if len(examples) < 8:
                examples.append((f["name"], (m[0].get("invoice_data") or {}).get("invoice_number"),
                                 m[0].get("vendor_name")))
        elif len(m) > 1:
            ambiguous += 1

    print(f"\n--- invoice_number-in-filename test ---")
    print(f"  usable invoice numbers : {len(numbers)}")
    print(f"  files matched uniquely : {hits} / {len(files)}")
    print(f"  files matching several : {ambiguous}")
    if examples:
        print("  examples:")
        for fn, num, vendor in examples:
            print(f"    {fn[:44]:46} -> {num}  ({vendor})")

    print("\nIf 'matched uniquely' is high, migrating by invoice number is viable.")
    print("If it is near zero, the historic files cannot be matched automatically.")


if __name__ == "__main__":
    main()
