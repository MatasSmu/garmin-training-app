"""Plan tab API: upload a coach's plan PDF, page images, dedupe, rename,
delete -- on a throwaway DB and plans folder."""

import base64
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config, plans
from app.api.deps import get_db
from app.db import init_db
from app.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(f"{config.APP_USERNAME}:{config.APP_PASSWORD}".encode()).decode()}


def make_pdf(pages: int = 2, text: str = "Plan") -> bytes:
    """A minimal valid PDF with `pages` pages of text (correct xref offsets)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)) + f"] /Count {pages} >>"]
    font = 3 + 2 * pages
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        stream = f"BT /F1 24 Tf 72 760 Td ({text} page {i + 1}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


@pytest.fixture()
def client(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "p.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    monkeypatch.setattr(plans, "PLANS_DIR", tmp_path / "plans")

    def _db():
        yield conn

    app.dependency_overrides[get_db] = _db
    with TestClient(app, headers=AUTH) as c:
        yield c
    app.dependency_overrides.clear()
    conn.close()


needs_poppler = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm (poppler-utils) not installed")


@needs_poppler
def test_upload_pages_dedupe_rename_delete(client, tmp_path):
    assert client.get("/api/plans").json() == []
    pdf = make_pdf(2)
    r = client.post("/api/plans", files={"file": ("Matas Šinkūnas.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["created"] is True and p["pages"] == 2 and p["title"] == "Matas Šinkūnas"
    assert p["file_name"] == "Matas Šinkūnas.pdf" and p["size_bytes"] == len(pdf)

    # the same file again is not stored twice
    again = client.post("/api/plans", files={"file": ("copy.pdf", pdf, "application/pdf")}).json()
    assert again["id"] == p["id"] and again["created"] is False
    assert len(client.get("/api/plans").json()) == 1

    # original PDF, inline or as a download
    r = client.get(f"/api/plans/{p['id']}/pdf")
    assert r.status_code == 200 and r.content == pdf and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get(f"/api/plans/{p['id']}/pdf?download=true").headers["content-disposition"].startswith("attachment")

    # page images
    for n in (1, 2):
        img = client.get(f"/api/plans/{p['id']}/pages/{n}.png")
        assert img.status_code == 200 and img.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert "immutable" in img.headers["cache-control"]
    assert client.get(f"/api/plans/{p['id']}/pages/3.png").status_code == 404
    assert client.get(f"/api/plans/{p['id']}/pages/0.png").status_code == 404

    # newest first, rename
    second = client.post("/api/plans", files={"file": ("next.pdf", make_pdf(1, "Next"), "application/pdf")},
                         data={"title": "December block"}).json()
    assert second["title"] == "December block" and second["pages"] == 1
    assert [x["id"] for x in client.get("/api/plans").json()] == [second["id"], p["id"]]
    assert client.patch(f"/api/plans/{p['id']}", json={"title": "Winter 8 weeks"}).json()["title"] == "Winter 8 weeks"
    assert client.patch(f"/api/plans/{p['id']}", json={"title": ""}).status_code == 422

    # delete removes the files too
    folder = tmp_path / "plans"
    assert (folder / f"{p['sha1']}.pdf").exists() and (folder / p["sha1"] / "page-2.png").exists()
    assert client.delete(f"/api/plans/{p['id']}").json() == {"deleted": p["id"]}
    assert not (folder / f"{p['sha1']}.pdf").exists() and not (folder / p["sha1"]).exists()
    assert client.get(f"/api/plans/{p['id']}/pdf").status_code == 404
    assert client.delete(f"/api/plans/{p['id']}").status_code == 404


def test_rejects_non_pdf_and_too_big(client, monkeypatch):
    r = client.post("/api/plans", files={"file": ("plan.pdf", b"hello", "application/pdf")})
    assert r.status_code == 400 and "not a PDF" in r.json()["detail"]
    monkeypatch.setattr(plans, "MAX_BYTES", 100)
    r = client.post("/api/plans", files={"file": ("big.pdf", b"%PDF" + b"0" * 200, "application/pdf")})
    assert r.status_code == 400 and "larger" in r.json()["detail"]
    assert client.get("/api/plans").json() == []


def test_pdf_without_poppler_still_works(client, monkeypatch):
    monkeypatch.setattr(plans.shutil, "which", lambda name: None)
    p = client.post("/api/plans", files={"file": ("plan.pdf", make_pdf(1), "application/pdf")}).json()
    assert p["pages"] == 0
    assert client.get(f"/api/plans/{p['id']}/pdf").status_code == 200
    assert client.get(f"/api/plans/{p['id']}/pages/1.png").status_code == 404


def test_requires_auth():
    with TestClient(app) as c:
        assert c.get("/api/plans").status_code == 401
