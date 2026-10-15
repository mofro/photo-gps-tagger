#!/usr/bin/env python3.13
"""
Diagnostic: compare what osxphotos sees in the DB vs what photoscript/AppleScript
can actually access. Run from terminal (not via server).

Usage:
    python3.13 /Users/mo/photo-edit/diag_uuid_check.py
"""
import sys

# Check a specific failing UUID from the apply log
FAILING_UUIDS = [
    "DC5F128A-63F1-494A-BE0D-1A4D105C128A",   # image000000.jpg
    "575A9CF7-253A-4D6F-8609-15464229799D",   # image000003.jpg
    "AB371C2A-A47F-49AE-B947-1640804F3DF3",   # IMG_3506.HEIC (prev session)
]

print("=== osxphotos side ===")
try:
    import osxphotos
    db = osxphotos.PhotosDB()
    all_photos = {p.uuid: p for p in db.photos(images=True)}
    print(f"osxphotos sees {len(all_photos)} photos total")
    for uuid in FAILING_UUIDS:
        p = all_photos.get(uuid)
        if p:
            print(f"  FOUND  {uuid}")
            print(f"         filename={p.original_filename}, shared={getattr(p,'shared',None)}, shared_library={getattr(p,'shared_library',None)}, ismissing={p.ismissing}")
        else:
            print(f"  MISSING {uuid}")
except Exception as e:
    print(f"osxphotos error: {e}")

print()
print("=== photoscript/AppleScript side ===")
try:
    import photoscript, time
    lib = photoscript.PhotosLibrary()
    lib.activate()
    time.sleep(3)

    # Count how many photos AppleScript sees
    try:
        all_as = lib.photos()
        print(f"AppleScript sees {len(all_as)} photos total")
        # Show first 5 UUIDs
        print("  First 5 AppleScript photo IDs:")
        for p in list(all_as)[:5]:
            try:
                print(f"    id={p.id}, filename={p.filename}")
            except Exception as e:
                print(f"    (error reading photo: {e})")
    except Exception as e:
        print(f"  lib.photos() failed: {e}")

    # Try each failing UUID
    print()
    print("  Direct UUID lookups:")
    for uuid in FAILING_UUIDS:
        try:
            photo = photoscript.Photo(uuid)
            print(f"  OK     {uuid} → {photo.filename}")
        except Exception as e:
            print(f"  FAIL   {uuid} → {e}")

except Exception as e:
    print(f"photoscript error: {e}")
