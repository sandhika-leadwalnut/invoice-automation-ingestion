"""
One-off migration: sort the existing flat Drive folder into vendor-wise subfolders.

Dry run by default. Nothing moves until you pass --apply.

    python migrate_drive_folders.py                    # report only
    python migrate_drive_folders.py --apply            # move confident matches
    python migrate_drive_folders.py --apply --limit 5  # move a few first

Matching strategies, tried in order and reported separately so you can see how
much rests on each:

  1. drive_file_id  - exact. Only present on invoices ingested after this change.
  2. pdf_filename   - exact. Only present on invoices ingested after this change.
  3. invoice_number - the number appears inside the filename. When several
                      numbers match, the LONGEST wins (so "2755" does not beat
                      "IDSL-2627-020"). If the longest is still tied and the
                      candidates disagree on vendor, the file is skipped.
  4. vendor_name    - a known vendor's name appears in the filename. Requires
                      a reasonably long name to avoid coincidental hits.

Anything unmatched is left in the root folder untouched. Filing an invoice under
the wrong vendor is worse than leaving it where it is.
"""

import argparse
import logging
import os
import re
import sys
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

from app.oauth import get_drive_service                      # noqa: E402
from app.drive_folders import (                              # noqa: E402
    FOLDER_MIME,
    get_or_create_folder,
    resolve_vendor_name,
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# Below this length, an invoice number or vendor name matches by coincidence.
MIN_NUMBER_LEN = 4
MIN_VENDOR_LEN = 8


def norm(s):
    """Lowercase alphanumerics only, so 'INV-001' and 'inv001' compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def meta_of(inv):
    data = inv.get("edited_data") or inv.get("invoice_data") or {}
    return {
        "gstin": data.get("vendor_gstin"),
        "vendor_name": data.get("vendor_name") or inv.get("vendor_name"),
        "vendor_id": data.get("vendor_id"),
    }


def load_invoices():
    r = requests.get(f"{BACKEND_URL}/verification/invoices/all", timeout=60)
    r.raise_for_status()
    return r.json()


def list_root_files(drive_service):
    files, token = [], None
    while True:
        resp = drive_service.files().list(
            q=f"'{ROOT_FOLDER_ID}' in parents and "
              f"mimeType != '{FOLDER_MIME}' and trashed = false",
            fields="nextPageToken, files(id, name, parents)",
            pageSize=200, pageToken=token,
        ).execute()
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def load_mapped_vendors():
    """
    Every vendor in invoice_db.vendors. Falls back to an empty list on older
    backends that lack the endpoint - matching then relies on invoice records only.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/verification/vendors/mapped", timeout=30)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"/vendors/mapped returned {r.status_code}; "
                       f"falling back to vendor names seen on invoices")
    except Exception as e:
        logger.warning(f"Could not load mapped vendors: {e}")
    return []


def stem(filename):
    """Filename without extension, normalised."""
    return norm(os.path.splitext(filename)[0])


def tokens(s):
    """Meaningful words in a name: lowercase, >=3 chars, no legal suffixes."""
    skip = {"pvt", "ltd", "llp", "limited", "private", "inc", "llc",
            "technologies", "services", "solutions", "india", "the", "and"}
    words = re.split(r"[^a-z0-9]+", str(s or "").lower())
    return [w for w in words if len(w) >= 3 and w not in skip]


def match_vendor_by_name(fn_stem, filename, vendor_names):
    """
    Match a filename against known vendor names, three ways:

      a) the vendor name appears inside the filename
         "Invoice For BizBoost Business Solutions LLP.pdf"
      b) the filename is an abbreviated prefix of the vendor name
         "data_driven.pdf" -> "Data Driven Services"
      c) every meaningful word in the filename appears in the vendor name
         "daiva fortune.pdf" -> "Daiva Fortune Enterprises"

    Returns a list of candidate original names.
    """
    hits = set()

    for nv, orig in vendor_names.items():
        # (a) vendor name contained in filename
        if nv and nv in fn_stem:
            hits.add(orig)
            continue
        # (b) filename is a prefix of the vendor name - needs to be long enough
        # that it is not a coincidence
        if len(fn_stem) >= 8 and nv.startswith(fn_stem):
            hits.add(orig)
            continue

    if hits:
        return list(hits)

    # (c) token subset, requiring at least two meaningful shared words
    ftok = set(tokens(filename))
    if len(ftok) >= 2:
        for nv, orig in vendor_names.items():
            vtok = set(tokens(orig))
            if ftok and ftok.issubset(vtok):
                hits.add(orig)

    return list(hits)


def build_indexes(invoices, mapped_vendors):
    by_id, by_name = {}, {}
    numbers = []          # (normalised_number, meta)
    vendor_names = {}     # normalised vendor name -> original

    # Canonical names first - these are the folders that should exist.
    for v in mapped_vendors:
        vn = v.get("vendor_name")
        if vn and len(norm(vn)) >= MIN_VENDOR_LEN:
            vendor_names[norm(vn)] = vn

    dupe_names = set()
    for inv in invoices:
        data = inv.get("edited_data") or inv.get("invoice_data") or {}
        meta = meta_of(inv)

        fid = inv.get("drive_file_id") or data.get("drive_file_id")
        if fid:
            by_id[fid] = meta

        fname = inv.get("pdf_filename") or data.get("pdf_filename")
        if fname:
            if fname in by_name and by_name[fname] != meta:
                dupe_names.add(fname)
            by_name.setdefault(fname, meta)

        num = norm(data.get("invoice_number"))
        if len(num) >= MIN_NUMBER_LEN:
            numbers.append((num, meta))

        vn = meta.get("vendor_name")
        if vn and len(norm(vn)) >= MIN_VENDOR_LEN:
            vendor_names[norm(vn)] = vn

    for n in dupe_names:
        by_name.pop(n, None)

    logger.info(
        f"Indexes: {len(by_id)} drive ids, {len(by_name)} filenames, "
        f"{len(numbers)} invoice numbers, {len(vendor_names)} vendor names"
    )
    return by_id, by_name, numbers, vendor_names


def match_file(f, by_id, by_name, numbers, vendor_names):
    """Return (meta_or_vendorname, strategy) or (None, reason)."""
    if f["id"] in by_id:
        return by_id[f["id"]], "drive_id"
    if f["name"] in by_name:
        return by_name[f["name"]], "filename"

    fn = norm(f["name"])

    # --- invoice number, longest match wins ---
    hits = [(num, meta) for num, meta in numbers if num in fn]
    if hits:
        longest = max(len(n) for n, _ in hits)
        best = [m for n, m in hits if len(n) == longest]
        vendors = {(m.get("gstin"), m.get("vendor_name")) for m in best}
        if len(vendors) == 1:
            return best[0], "invoice_number"
        return None, "ambiguous_number"

    # --- vendor name in the filename ---
    # No GSTIN is involved here, so ambiguity is never resolved by guessing.
    # More than one candidate means the file stays in the root.
    vhits = match_vendor_by_name(stem(f["name"]), f["name"], vendor_names)
    if len(vhits) == 1:
        return {"gstin": None, "vendor_name": vhits[0], "vendor_id": None}, "vendor_name"
    if len(vhits) > 1:
        logger.debug(f"{f['name']}: matches {len(vhits)} vendors {vhits} - skipping")
        return None, "ambiguous_vendor"

    return None, "no_match"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually move files")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--strategies", default="drive_id,filename,invoice_number,vendor_name",
                    help="Comma-separated subset to trust")
    args = ap.parse_args()

    if not ROOT_FOLDER_ID:
        sys.exit("GOOGLE_DRIVE_FOLDER_ID is not set")

    allowed = {s.strip() for s in args.strategies.split(",") if s.strip()}

    drive = get_drive_service()
    invoices = load_invoices()
    mapped = load_mapped_vendors()
    logger.info(f"Loaded {len(invoices)} invoices, {len(mapped)} mapped vendors")

    by_id, by_name, numbers, vendor_names = build_indexes(invoices, mapped)
    files = list_root_files(drive)
    logger.info(f"Found {len(files)} files in the root folder")
    if args.limit:
        files = files[: args.limit]

    stats = Counter()
    planned = []
    unresolved = []

    # Cache vendor resolution so we do not hammer the backend per file.
    resolved_cache = {}

    for f in files:
        meta, strategy = match_file(f, by_id, by_name, numbers, vendor_names)
        if not meta:
            stats[strategy] += 1
            continue
        if strategy not in allowed:
            stats[f"skipped_{strategy}"] += 1
            continue

        key = (meta.get("gstin"), meta.get("vendor_name"), meta.get("vendor_id"))
        if key not in resolved_cache:
            resolved_cache[key] = resolve_vendor_name(
                gstin=meta.get("gstin"),
                vendor_name=meta.get("vendor_name"),
                vendor_id=meta.get("vendor_id"),
            )
        canonical = resolved_cache[key]

        if not canonical:
            stats["vendor_unmapped"] += 1
            unresolved.append((f["name"], meta.get("vendor_name")))
            continue

        planned.append((f, canonical, strategy))
        stats[f"match_{strategy}"] += 1

    print("\n--- Plan ---")
    for f, vendor, strategy in planned[:40]:
        print(f"  [{strategy:14}] {f['name'][:46]:48} -> {vendor}")
    if len(planned) > 40:
        print(f"  ... and {len(planned) - 40} more")

    print("\n--- Matched ---")
    for s in ("drive_id", "filename", "invoice_number", "vendor_name"):
        if stats[f"match_{s}"]:
            print(f"  {s:16} {stats[f'match_{s}']}")
    print(f"  TOTAL            {len(planned)} / {len(files)}")

    print("\n--- Not matched ---")
    for k in ("no_match", "ambiguous_number", "ambiguous_vendor", "vendor_unmapped"):
        if stats[k]:
            print(f"  {k:18} {stats[k]}")

    if unresolved:
        print("\n  vendors seen but not in invoice_db.vendors:")
        for v in sorted({v for _, v in unresolved if v})[:12]:
            print(f"    - {v}")

    by_vendor = Counter(v for _, v, _ in planned)
    print(f"\n  distinct vendor folders to create: {len(by_vendor)}")

    if not args.apply:
        print("\nDry run - nothing moved. Re-run with --apply to execute.")
        return

    print("\nMoving...")
    moved = failed = 0
    for f, vendor, _ in planned:
        try:
            folder_id = get_or_create_folder(drive, vendor, ROOT_FOLDER_ID)
            prev = ",".join(f.get("parents", [ROOT_FOLDER_ID]))
            drive.files().update(
                fileId=f["id"], addParents=folder_id,
                removeParents=prev, fields="id, parents",
            ).execute()
            moved += 1
            logger.info(f"Moved {f['name']} -> {vendor}")
        except Exception as e:
            failed += 1
            logger.error(f"Failed to move {f['name']}: {e}")

    print(f"\nDone. moved={moved} failed={failed}")
    print("Unmatched files were left in the root folder untouched.")


if __name__ == "__main__":
    main()
