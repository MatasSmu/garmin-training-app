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
    with pytest.raises(sheets.SheetError, match="date and a weight column"):
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


def test_score_read_as_displayed_when_sheets_made_it_a_date():
    # Lithuanian locale: a typed 8.5 becomes 5 August (serial 46239) but still shows "8.5".
    rows = [["Weight (kg)", "Date", "Time", "Rating", "Notes"],
            ["81.9", 46328, 0.3257, 46239, "nenorėjau keltis"],
            ["81.1", 46327, 0.36, 8, ""],
            ["80.0", 46326, 0.3, "6.5 / 6:33", ""]]
    shown = [rows[0], ["81.9", "2026-11-02", "07:49", "8.5", "nenorėjau keltis"],
             ["81.1", "2026-11-01", "08:38", "8", ""], ["80.0", "2026-10-31", "07:12", "6.5 / 6:33", ""]]
    entries, _, _ = sheets.parse_rows(rows, "lt_LT", shown)
    assert sorted((e["date"], e["score"]) for e in entries) == [("2026-10-31", 6.5), ("2026-11-01", 8.0), ("2026-11-02", 8.5)]


def test_after_midnight_is_previous_evening_and_second_morning_is_evening():
    rows = [["Weight (kg)", "Date", "Time", "Rating", "Notes"],
            ["82.5", "2026-09-30", "00:10", "", "post-midnight 09-29 night"],
            ["81.3", "2026-09-30", "09:15", "8.5", ""],
            ["82.4", "2026-09-26", "08:41", "8", ""],
            ["82.7", "2026-09-26", "13:10", "", ""],
            ["80.0", "2026-09-27", "07:00", "", ""],
            ["80.1", "2026-09-27", "09:00", "", "re-weighed"],
            ["81.0", "2026-09-27", "21:00", "", ""]]
    entries, skipped, _ = sheets.parse_rows(rows)
    got = sorted((e["date"], e["slot"], e["time"], e["weight_kg"]) for e in entries)
    assert got == [
        ("2026-09-26", "evening", "13:10", 82.7), ("2026-09-26", "morning", "08:41", 82.4),
        ("2026-09-27", "evening", "21:00", 81.0), ("2026-09-27", "morning", "07:00", 80.0),
        ("2026-09-29", "evening", "00:10", 82.5), ("2026-09-30", "morning", "09:15", 81.3),
    ]
    # 27th: 07:00 morning, 09:00 took the free evening, then 21:00 replaced it.
    assert [(s["row"], s["reason"]) for s in skipped] == [(7, "two evening weigh-ins on 2026-09-27; kept row 8")]


def test_auto_import_twice_a_day_with_catch_up_and_retry():
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=2))
    at = lambda d, hm: datetime.fromisoformat(f"2026-11-{d}T{hm}:00").replace(tzinfo=tz)  # noqa: E731
    times = ["10:00", "23:30"]
    assert sheets.last_slot(at("03", "09:59"), times) == at("02", "23:30")
    assert sheets.last_slot(at("03", "10:00"), times) == at("03", "10:00")
    ok = at("03", "10:01")
    assert not sheets.is_due(at("03", "15:00"), ok, ok, times)          # done this morning
    assert sheets.is_due(at("03", "23:31"), ok, ok, times)              # evening slot passed
    assert sheets.is_due(at("04", "08:00"), ok, ok, times)              # laptop was off at 23:30: catch up
    # A failed attempt (offline) retries after 30 min, not every 5.
    assert not sheets.is_due(at("03", "23:50"), ok, at("03", "23:31"), times)
    assert sheets.is_due(at("04", "00:02"), ok, at("03", "23:31"), times)
    assert sheets.is_due(at("03", "12:00"), None, None, times)          # never imported
    assert not sheets.is_due(at("03", "12:00"), None, None, [])         # disabled


# The athlete's real layout: weight, date, time, score, note -- no header row.
REAL_ROWS = [
    ["80.4", "2026-09-04", "22:40", "", "(ryte atsisėdimai)"],
    ["79.7", "2026-09-05", "07:24", "8.9", "neblogai išsimiegojau"],
    ["79.9", "2026-09-06", "23:04", "", "pavargęs, gerai"],
]
EXPECTED = [
    ("2026-09-04", "evening", "22:40", 80.4, None, "(ryte atsisėdimai)"),
    ("2026-09-05", "morning", "07:24", 79.7, 8.9, "neblogai išsimiegojau"),
    ("2026-09-06", "evening", "23:04", 79.9, None, "pavargęs, gerai"),
]


def _serial(iso):
    return (date.fromisoformat(iso) - sheets.EPOCH).days


@pytest.mark.parametrize("rows, first_col_name", [
    (REAL_ROWS, "column A"),
    # What the Sheets API returns for real date/time/number cells (UNFORMATTED_VALUE).
    ([[80.4, _serial("2026-09-04"), 22 / 24 + 40 / 1440, "", "(ryte atsisėdimai)"],
      [79.7, _serial("2026-09-05"), 7 / 24 + 24 / 1440, 8.9, "neblogai išsimiegojau"],
      [79.9, _serial("2026-09-06"), 23 / 24 + 4 / 1440]], "column A"),
    # A header row whose words the importer doesn't know is ignored.
    ([["Kg?", "Kada", "Val", "Kaip", "Kita"]] + REAL_ROWS, "column A"),
    # A known header for some columns; the rest are recognised from content.
    ([["Svoris", "Data", "", "", ""]] + REAL_ROWS, "Svoris"),
])
def test_columns_recognised_from_content(rows, first_col_name):
    entries, skipped, cols = sheets.parse_rows(rows)
    assert skipped == []
    assert cols["weight"] == first_col_name
    assert (cols["time"], cols["score"], cols["note"]) == ("column C", "column D", "column E")
    got = sorted((e["date"], e["slot"], e["time"], e["weight_kg"], e["score"], e["note"]) for e in entries)
    expected = EXPECTED if len(rows[-1]) == 5 else EXPECTED[:2] + [EXPECTED[2][:5] + (None,)]
    assert got == expected
