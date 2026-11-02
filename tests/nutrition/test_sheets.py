"""Google Sheet import: header detection, date/time/slot parsing, the
change-only import (an unchanged sheet row never overwrites an app edit) and
the API. Google itself is faked."""

import json
from datetime import date

import pytest

from app.nutrition import sheets, store

SERIAL_2026_11_02 = (date(2026, 11, 2) - sheets.EPOCH).days


def test_columns_found_by_header_in_english_and_lithuanian():
    assert sheets.find_columns(["Date", "Time", "Weight (kg)", "Morning score", "Note"]) == \
        {"date": 0, "time": 1, "weight": 2, "score": 3, "note": 4}
    assert sheets.find_columns(["Data", "Laikas", "Svoris", "Savijauta", "Pastabos"]) == \
        {"date": 0, "time": 1, "weight": 2, "score": 3, "note": 4}
    assert sheets.find_columns(["Pastaba", "Svoris", "Data"]) == {"note": 0, "weight": 1, "date": 2}


@pytest.mark.parametrize("v, month_first, expected", [
    (SERIAL_2026_11_02, False, (date(2026, 11, 2), None)),
    (SERIAL_2026_11_02 + 0.3125, False, (date(2026, 11, 2), "07:30")),
    ("2026-11-02", False, (date(2026, 11, 2), None)),
    ("2026.11.02 19:05", False, (date(2026, 11, 2), "19:05")),
    ("02.11.2026", False, (date(2026, 11, 2), None)),
    ("2.11.26", False, (date(2026, 11, 2), None)),
    ("02/11/2026", False, (date(2026, 11, 2), None)),
    ("11/02/2026", True, (date(2026, 11, 2), None)),
    ("25/10/2026", True, (date(2026, 10, 25), None)),   # can't be month-first
    ("soon", False, (None, None)),
    ("", False, (None, None)),
])
def test_parse_date(v, month_first, expected):
    assert sheets.parse_date(v, month_first) == expected


@pytest.mark.parametrize("v, expected", [
    (0.3125, ("07:30", None)), ("7:30", ("07:30", None)), ("19.45", ("19:45", None)),
    ("7:30 PM", ("19:30", "evening")), ("rytas", (None, "morning")), ("Vakare", (None, "evening")), (None, (None, None)),
])
def test_parse_time(v, expected):
    assert sheets.parse_time(v) == expected


def test_parse_rows_slots_from_time_order_and_words():
    rows = [
        ["My weight log"],                                   # a title row above the header is fine
        ["Data", "Laikas", "Svoris", "Ryto įvertinimas", "Pastaba"],
        ["2026-11-01", "07:10", "81,4", 7, "slept well"],
        ["2026-11-01", "21:30", 82.3, "", ""],
        ["2026-11-02", "", 81.2, "6/10", ""],              # no time: 1st of day = morning
        ["2026-11-02", "", 82.0],                           # 2nd = evening
        ["2026-11-02", "", 82.1],                           # 3rd: can't place
        [],
        ["2026-11-03", "vakaras", 82.4],
        ["bad date", "07:00", 81],
        ["2026-11-04", "07:00", ""],
        ["2026-11-05", "07:00", 8.1],
    ]
    entries, skipped, cols = sheets.parse_rows(rows)
    got = {(e["date"], e["slot"]): e for e in entries}
    assert cols == {"date": "Data", "time": "Laikas", "weight": "Svoris", "score": "Ryto įvertinimas", "note": "Pastaba"}
    assert got[("2026-11-01", "morning")] == {"date": "2026-11-01", "slot": "morning", "time": "07:10", "weight_kg": 81.4,
                                             "score": 7.0, "note": "slept well", "row": 3}
    assert got[("2026-11-01", "evening")]["time"] == "21:30" and got[("2026-11-01", "evening")]["score"] is None
    assert got[("2026-11-02", "morning")]["score"] == 6.0 and got[("2026-11-02", "evening")]["weight_kg"] == 82.0
    assert got[("2026-11-03", "evening")]["weight_kg"] == 82.4
    assert [s["row"] for s in skipped] == [7, 10, 11, 12]


def test_parse_rows_without_header_is_a_clear_error():
    with pytest.raises(sheets.SheetError, match="header row"):
        sheets.parse_rows([["a", "b"], [1, 2]])


def test_sheet_url():
    sid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert sheets.parse_sheet_url(f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid=123") == (sid, 123)
    assert sheets.parse_sheet_url(f"https://docs.google.com/spreadsheets/d/{sid}/edit?usp=sharing") == (sid, None)
    with pytest.raises(sheets.SheetError):
        sheets.parse_sheet_url("https://example.com/x")


def _entry(**kw):
    e = {"date": "2026-11-01", "slot": "morning", "time": "07:10", "weight_kg": 81.4, "score": 7.0, "note": "ok"}
    e.update(kw)
    return e


def test_import_adds_then_only_changed_rows_and_keeps_app_edits(conn):
    assert sheets.apply_entries(conn, [_entry(), _entry(slot="evening", weight_kg=82.3)]) == \
        {"added": 2, "updated": 0, "unchanged": 0}
    assert sheets.apply_entries(conn, [_entry(), _entry(slot="evening", weight_kg=82.3)])["unchanged"] == 2
    # The athlete corrects the morning in the app: an unchanged sheet row must not undo that.
    store.upsert_weight(conn, "2026-11-01", "morning", 81.0)
    conn.commit()
    assert sheets.apply_entries(conn, [_entry()])["unchanged"] == 1
    row = next(r for r in store.list_weights(conn) if r["slot"] == "morning")
    assert (row["weight_kg"], row["score"], row["note"], row["time"]) == (81.0, 7.0, "ok", "07:10")
    # Changing the row in the sheet does win.
    assert sheets.apply_entries(conn, [_entry(score=8.0)])["updated"] == 1
    row = next(r for r in store.list_weights(conn) if r["slot"] == "morning")
    assert (row["weight_kg"], row["score"]) == (81.4, 8.0)


def test_grid_save_keeps_sheet_note_and_score(client, conn):
    sheets.apply_entries(conn, [_entry()])
    r = client.put("/api/nutrition/weights", json={"entries": [{"date": "2026-11-01", "slot": "morning", "weight_kg": 81.6}]})
    assert r.json()["results"]["2026-11-01|morning"] == "updated"
    row = client.get("/api/nutrition/weights").json()[0]
    assert (row["weight_kg"], row["score"], row["note"], row["time"]) == (81.6, 7.0, "ok", "07:10")
    # The check-in card sends score and note explicitly (null clears).
    client.post("/api/nutrition/sync", json={"weights": [
        {"date": "2026-11-01", "slot": "morning", "weight_kg": 81.6, "score": 5, "note": None}]})
    row = client.get("/api/nutrition/checkins?days=3650").json()[0]
    assert (row["score"], row["note"]) == (5.0, None)


def test_sheet_api_end_to_end_with_fake_google(client, conn, tmp_path, monkeypatch):
    key = tmp_path / "key.json"
    monkeypatch.setattr(sheets, "KEY_PATH", key)
    st = client.get("/api/nutrition/sheet").json()
    assert st["key_present"] is False and st["configured"] is False

    key.write_text(json.dumps({"type": "service_account", "client_email": "reader@p.iam.gserviceaccount.com"}))
    assert client.put("/api/nutrition/sheet", json={"url": "https://example.com/nope"}).status_code == 422
    sid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    st = client.put("/api/nutrition/sheet", json={"url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid=5"}).json()
    assert st["configured"] and st["service_account_email"] == "reader@p.iam.gserviceaccount.com"

    calls = []

    def fake_get(url, params=None):
        calls.append(url)
        if url.endswith(sid):
            return {"properties": {"locale": "lt_LT"}, "sheets": [{"properties": {"sheetId": 0, "title": "Other"}},
                                                                 {"properties": {"sheetId": 5, "title": "Svoris"}}]}
        return {"values": [["Data", "Laikas", "Svoris", "Savijauta", "Pastaba"],
                           [SERIAL_2026_11_02, 0.3, 81.2, 7, "good"], [SERIAL_2026_11_02, 0.9, 82.0]]}

    monkeypatch.setattr(sheets, "_get", fake_get)
    r = client.post("/api/nutrition/sheet/import").json()
    assert r["result"]["ok"], r
    assert (r["result"]["added"], r["result"]["tab"]) == (2, "Svoris")
    assert calls[1].endswith("/values/%27Svoris%27")
    assert r["status"]["last"]["message"].startswith("Tab “Svoris”: 2 added")
    rows = client.get("/api/nutrition/checkins?days=3650").json()
    assert [(x["slot"], x["time"], x["score"]) for x in rows] == [("morning", "07:12", 7.0), ("evening", "21:36", None)]


def test_import_reports_google_errors(conn, monkeypatch):
    sheets.set_setting(conn, "sheet", {"id": "x" * 30})

    def denied(url, params=None):
        raise sheets.SheetError("No access: share the sheet with reader@p (Viewer).")

    monkeypatch.setattr(sheets, "_get", denied)
    res = sheets.run_import(conn)
    assert res["ok"] is False and "share the sheet" in res["message"]
    assert sheets.get_setting(conn, "sheet_last_import")["ok"] is False
