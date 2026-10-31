"""The coach's plan documents (PDF) for the Plan tab.

The PDF is kept as uploaded and each page is also rendered to a PNG
(poppler's pdftoppm), because Chrome on Android can't show a PDF inside a
page. Files live next to the database in plans/<sha1>.pdf and
plans/<sha1>/page-N.png, so tests and throwaway servers (APP_DB_PATH) get
their own folder. Uploading the same file twice is a no-op.

    venv/bin/python -m app.plans "path/to/plan.pdf" [--title "8 week plan"]
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import DB_PATH

PLANS_DIR = DB_PATH.parent / "plans"
MAX_BYTES = 25 * 1024 * 1024
PAGE_DPI = 110          # ~900 px wide for an A4 page: sharp on a phone, ~100-200 kB per page


class BadPlan(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pdf_path(sha1: str) -> Path:
    return PLANS_DIR / f"{sha1}.pdf"


def page_path(sha1: str, n: int) -> Path:
    return PLANS_DIR / sha1 / f"page-{n}.png"


def render_pages(sha1: str) -> int:
    """Render every page to PNG; returns the page count (0 when pdftoppm is
    missing -- the PDF itself still works)."""
    if shutil.which("pdftoppm") is None:
        return 0
    out = PLANS_DIR / sha1
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(PAGE_DPI), str(pdf_path(sha1)), str(out / "p")],
                   check=True, capture_output=True, timeout=120)
    # pdftoppm pads page numbers to the page count's width (p-1 or p-01); normalise
    pages = sorted(out.glob("p-*.png"), key=lambda p: int(p.stem.split("-")[1]))
    for i, p in enumerate(pages, 1):
        p.replace(out / f"page-{i}.png")
    return len(pages)


def add_plan(conn: sqlite3.Connection, data: bytes, file_name: str, title: str | None = None) -> tuple[int, bool]:
    """Store a plan PDF. Returns (id, created)."""
    if len(data) > MAX_BYTES:
        raise BadPlan(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data.startswith(b"%PDF"):
        raise BadPlan("not a PDF file")
    sha1 = hashlib.sha1(data).hexdigest()
    row = conn.execute("SELECT id FROM plan_documents WHERE sha1 = ?", (sha1,)).fetchone()
    if row:
        return row["id"], False
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path(sha1).write_bytes(data)
    try:
        pages = render_pages(sha1)
    except (subprocess.SubprocessError, OSError) as e:
        pdf_path(sha1).unlink(missing_ok=True)
        shutil.rmtree(PLANS_DIR / sha1, ignore_errors=True)
        raise BadPlan(f"couldn't read the PDF ({e.__class__.__name__})") from e
    name = Path(file_name).name or "plan.pdf"
    cur = conn.execute(
        "INSERT INTO plan_documents (title, file_name, sha1, pages, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
        ((title or "").strip() or Path(name).stem, name, sha1, pages, len(data), _now()),
    )
    conn.commit()
    return cur.lastrowid, True


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM plan_documents ORDER BY uploaded_at DESC, id DESC")]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM plan_documents WHERE id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def rename_plan(conn: sqlite3.Connection, plan_id: int, title: str) -> bool:
    cur = conn.execute("UPDATE plan_documents SET title = ? WHERE id = ?", (title.strip(), plan_id))
    conn.commit()
    return cur.rowcount > 0


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> bool:
    plan = get_plan(conn, plan_id)
    if plan is None:
        return False
    conn.execute("DELETE FROM plan_documents WHERE id = ?", (plan_id,))
    conn.commit()
    pdf_path(plan["sha1"]).unlink(missing_ok=True)
    shutil.rmtree(PLANS_DIR / plan["sha1"], ignore_errors=True)
    return True


def main(argv: list[str]) -> None:
    import argparse

    from app.db import get_connection, init_db

    ap = argparse.ArgumentParser(description="Add a coach's plan PDF to the Plan tab")
    ap.add_argument("pdf")
    ap.add_argument("--title")
    args = ap.parse_args(argv)
    conn = get_connection()
    init_db(conn)
    path = Path(args.pdf)
    plan_id, created = add_plan(conn, path.read_bytes(), path.name, args.title)
    plan = get_plan(conn, plan_id)
    print(f"{'Added' if created else 'Already there'}: #{plan_id} {plan['title']!r}, {plan['pages']} page(s)")


if __name__ == "__main__":
    main(sys.argv[1:])
