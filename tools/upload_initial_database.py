from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cloud_store


def main() -> None:
    url = os.environ.get("SUPABASE_URL", "").strip() or input("Supabase URL: ").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip() or getpass.getpass("Service role key: ").strip()
    bucket = os.environ.get("SUPABASE_BUCKET", "student-files").strip()
    cloud_store.configure(url, key, bucket)
    if not cloud_store.is_cloud():
        raise SystemExit("Supabase URL or service role key is missing")

    local_root = ROOT / "data"
    db_path = local_root / "scores.db"
    if not db_path.exists():
        raise SystemExit(f"Local database not found: {db_path}")
    print("Uploading database...")
    cloud_store.upload_local_file(db_path, "database/scores.db")

    photo_dir = local_root / "photos"
    uploaded = 0
    if photo_dir.exists():
        for path in photo_dir.iterdir():
            if path.is_file():
                print(f"Uploading photo: {path.name}")
                cloud_store.upload_photo(path, path.name)
                uploaded += 1
    print(f"Done. Database uploaded. Files uploaded: {uploaded}")


if __name__ == "__main__":
    main()