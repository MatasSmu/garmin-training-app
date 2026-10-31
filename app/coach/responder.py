"""Turns a computed context dict into a chat reply.

Tries DeepSeek first, so the coach is a real back-and-forth conversation
grounded in the athlete's actual data (see _context_briefing / SYSTEM_PROMPT
below). Falls back to the fixed templates when no API key is configured or
the call fails, so the app keeps working offline / without a key.
engine.py, intents.py, and metrics.py are untouched by this.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date
from typing import Callable

import httpx

from app.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from app.coach.intents import EXAMPLE_QUESTIONS
from app.coach.tools import TOOL_SPECS, run_tool

logger = logging.getLogger(__name__)


def _should_train_today(ctx: dict) -> str:
    # days_since_rest counts back from today for the most recent day with no
    # session; 0 means today itself has none logged yet (i.e. today is
    # currently an unbroken rest day), N means you've trained N days running.
    days_rest = ctx.get("days_since_rest")
    load = ctx.get("load_7d", {})
    sleep = ctx.get("sleep_trend", {})

    parts = []
    if days_rest is not None:
        parts.append(f"You've trained {days_rest} day{'s' if days_rest != 1 else ''} in a row.")
    if load.get("session_count") is not None:
        parts.append(
            f"Last 7 days: {load['session_count']} sessions, "
            f"{load.get('total_duration_min', 0):.0f} min total."
        )
    if sleep.get("delta_pct") is not None:
        direction = "below" if sleep["delta_pct"] < 0 else "above"
        parts.append(f"Sleep is running {abs(sleep['delta_pct']):.0f}% {direction} your 28-day baseline.")

    today_events, race_tomorrow = _today_from_calendar(ctx.get("calendar") or {})
    blocked = [ev for ev in today_events if ev["kind"] == "blocked"]
    races = [ev for ev in today_events if ev["kind"] == "race"]
    planned = [ev for ev in today_events if ev["kind"] == "training"]

    if blocked:
        why = ", ".join(f"{ev['title']} ({ev.get('reason', 'other')})" for ev in blocked)
        verdict = f"Your calendar says you can't train today: {why}. Take it as a rest day."
    elif races:
        verdict = f"It's race day: {races[0]['title']}. Warm up well and keep everything else light."
    elif race_tomorrow:
        verdict = f"You race tomorrow ({race_tomorrow['title']}) -- keep today short and easy."
    elif days_rest is not None and days_rest >= 5:
        verdict = "You're due a rest day -- consider taking it easy today."
    elif sleep.get("delta_pct") is not None and sleep["delta_pct"] <= -15:
        verdict = "Sleep has been noticeably short lately -- an easy day would help recovery."
    else:
        verdict = "Nothing here suggests you need to hold back -- training today looks reasonable."

    if planned and not blocked and not races:
        parts.append("Planned for today: " + "; ".join(ev["title"] for ev in planned) + ".")
    return " ".join(parts + [verdict]) if parts else verdict


def _today_from_calendar(cal: dict) -> tuple[list[dict], dict | None]:
    events = cal.get("events") or []
    today = [ev for ev in events if ev.get("status") in ("today", "ongoing")]
    race_tomorrow = next(
        (ev for ev in events if ev["kind"] == "race" and ev.get("in_days") == 1), None
    )
    return today, race_tomorrow


def _upcoming(ctx: dict) -> str:
    cal = ctx.get("calendar") or {}
    ahead = [ev for ev in cal.get("events") or [] if ev.get("status") != "past"]
    if not ahead and not cal.get("later_races"):
        return "Nothing in your calendar for the next 4 weeks. Add trainings, races and busy days in the Calendar tab."
    lines = ["Coming up in your calendar:"]
    lines.extend(f"- {_event_line({**ev, 'id': None})}" for ev in ahead)
    lines.extend(f"Later race: {_race_line(r)}" for r in cal.get("later_races") or [])
    return "\n".join(lines)


def _week_summary(ctx: dict) -> str:
    load_7d = ctx.get("load_7d", {})
    load_28d = ctx.get("load_28d", {})
    journal = ctx.get("journal", [])

    lines = [
        f"This week: {load_7d.get('session_count', 0)} sessions, "
        f"{load_7d.get('total_duration_min', 0):.0f} min, "
        f"{load_7d.get('total_distance_km', 0):.1f} km."
    ]
    if load_28d.get("session_count"):
        weekly_avg = load_28d["session_count"] / 4
        lines.append(f"Your 4-week average is about {weekly_avg:.1f} sessions/week.")
    if journal:
        lines.append(f"You logged {len(journal)} journal note(s) this week.")
    return " ".join(lines)


def _sleep_check(ctx: dict) -> str:
    sleep = ctx.get("sleep_trend", {})
    if sleep.get("avg_sleep_h") is None:
        return "No sleep data logged for the last 7 days yet."
    line = f"You've averaged {sleep['avg_sleep_h']:.1f}h of sleep over the last 7 days"
    if sleep.get("baseline_avg_sleep_h") is not None:
        line += f", vs a {sleep['baseline_avg_sleep_h']:.1f}h 28-day baseline"
        if sleep.get("delta_pct") is not None:
            direction = "down" if sleep["delta_pct"] < 0 else "up"
            line += f" ({direction} {abs(sleep['delta_pct']):.0f}%)"
    return line + "."


def _streak(ctx: dict) -> str:
    streak = ctx.get("streak", {})
    if not streak:
        return "No session history to compute a streak from yet."
    kind = "training" if streak.get("type") == "training" else "rest"
    return f"You're on a {streak.get('days', 0)}-day {kind} streak."


def _load_check(ctx: dict) -> str:
    load_7d = ctx.get("load_7d", {})
    load_28d = ctx.get("load_28d", {})
    lines = [
        f"Last 7 days: {load_7d.get('session_count', 0)} sessions, "
        f"{load_7d.get('total_duration_min', 0):.0f} min, "
        f"{load_7d.get('total_distance_km', 0):.1f} km."
    ]
    if load_28d:
        lines.append(
            f"Last 28 days: {load_28d.get('session_count', 0)} sessions, "
            f"{load_28d.get('total_duration_min', 0):.0f} min."
        )
    return " ".join(lines)


def _help(_ctx: dict) -> str:
    questions = "\n".join(f"- {q}" for q in EXAMPLE_QUESTIONS)
    return f"I didn't catch that. Try one of these:\n{questions}"


RESPONSE_BUILDERS: dict[str, Callable[[dict], str]] = {
    "should_train_today": _should_train_today,
    "upcoming": _upcoming,
    "week_summary": _week_summary,
    "sleep_check": _sleep_check,
    "streak": _streak,
    "load_check": _load_check,
}


KIND_TEXT = {"training": "PLANNED TRAINING", "race": "RACE", "plan": "PLAN", "blocked": "CAN'T TRAIN"}


def _event_line(ev: dict) -> str:
    """One calendar event as a plain line, e.g.
    'Sat 2026-09-26 (in 2 days) RACE: Regatta -- bring 7.8 sail'."""
    def day(iso: str) -> str:
        return date.fromisoformat(iso).strftime("%a %Y-%m-%d")

    when = day(ev["date"]) + (f" to {day(ev['end_date'])}" if ev.get("end_date") else "")
    if ev.get("time"):
        when += f" {ev['time']}"
    status = ev.get("status")
    if status == "upcoming":
        n = ev["in_days"]
        when += " (tomorrow)" if n == 1 else f" (in {n} days)"
    elif status in ("today", "ongoing"):
        when += f" ({status})"
    elif status == "past" and "done" in ev:
        when += " (past -- a session WAS logged that day)" if ev["done"] else " (past -- NO session logged that day)"
    elif status == "past":
        when += " (past)"
    kind = KIND_TEXT.get(ev["kind"], ev["kind"])
    if ev.get("reason"):
        kind += f" ({ev['reason']})"
    line = f"{when} {kind}: {ev['title']}"
    if ev.get("id") is not None:
        line = f"[#{ev['id']}] {line}"
    if ev.get("notes"):
        line += f" -- {ev['notes']}"
    return line


def _calendar_briefing(cal: dict) -> list[str]:
    events = cal.get("events") or []
    lines = []
    if events:
        lines.append("- Athlete's calendar, last 7 days to 4 weeks ahead (their own entries):")
        lines.extend(f"  - {_event_line(ev)}" for ev in events)
    else:
        lines.append("- Athlete's calendar: nothing marked from last week to 4 weeks ahead.")
    later = cal.get("later_races") or []
    if later:
        lines.append("- Races further out:")
        lines.extend(f"  - {_race_line(r)}" for r in later)
    elif not any(ev["kind"] == "race" and ev.get("status") != "past" for ev in events):
        lines.append("- No upcoming race in the calendar.")
    return lines


def _race_line(race: dict) -> str:
    line = f"{race['title']} on {race['date']}"
    if race.get("end_date"):
        line += f" to {race['end_date']}"
    line += f" (in {race['in_days']} days)"
    if race.get("notes"):
        line += f" -- {race['notes']}"
    return line


def _sailing_focus_briefing(focus: dict) -> list[str]:
    """What the per-session iQFOIL analyses asked to work on next -- the
    input for planning the week's sailing."""
    lines = []
    sessions = focus.get("sessions") or []
    if sessions:
        lines.append("- Sailing focus from recent iQFOIL session analyses (newest first):")
        for s in sessions:
            where = f" at {s['spot']}" if s.get("spot") else ""
            band = f", {s['band_kn']} kn" if s.get("band_kn") else ""
            lines.append(f"  - {s['date']}{where}{band}: focus next: " + "; ".join(s["focus_next_session"])
                         + (f" | plan: {s['plan_adjustment']}" if s.get("plan_adjustment") else ""))
    week = focus.get("week")
    if week and week.get("focus_next_week"):
        lines.append(f"- Weekly sailing summary (week of {week['week_start']}) focus: "
                     + "; ".join(week["focus_next_week"])
                     + (f" | plan: {week['plan_adjustment']}" if week.get("plan_adjustment") else ""))
    return lines


def _gym_briefing(gym: dict) -> list[str]:
    """Coach's gym plan and the last few logged workouts, one line each."""
    if not gym:
        return []
    week = gym.get("week_now")
    when = (f"week {week} of {gym['weeks']}" if week and 1 <= week <= (gym.get("weeks") or 0)
            else f"starts {gym['start_date']}" if week is not None and week < 1 else "finished")
    lines = [f"- Gym plan '{gym['plan']}' ({when}); days: {', '.join(gym['schedule'])}."]
    if gym.get("sessions"):
        lines.append("- Recent gym workouts from the gym tracker (newest first; sets done/planned, "
                     "reps done/plan minimum; top = heaviest set per exercise):")
        for s in gym["sessions"]:
            bits = [f"{s['date']} day {s['day']}", s.get("location"),
                    f"{s['duration_min']} min" if s.get("duration_min") is not None else "in progress",
                    f"sets {s['sets']}", f"reps {s['reps']}", f"volume {s['volume_kg']} kg"]
            if s.get("avg_hr"):
                bits.append(f"avg HR {round(s['avg_hr'])}")
            line = "  - " + ", ".join(b for b in bits if b)
            if s.get("top"):
                line += " | top: " + "; ".join(s["top"])
            if s.get("notes"):
                line += f" | note: {s['notes']}"
            lines.append(line)
    else:
        lines.append("- No gym workouts logged in the gym tracker yet.")
    return lines


def _context_briefing(ctx: dict) -> str:
    """Render the full metrics context as plain-language facts for the
    DeepSeek system prompt."""
    load_7d = ctx.get("load_7d", {})
    load_28d = ctx.get("load_28d", {})
    sleep = ctx.get("sleep_trend", {})
    streak = ctx.get("streak", {})
    journal = ctx.get("journal", [])

    lines = [
        f"- Current streak: {streak.get('days', 0)} day(s) of {streak.get('type', 'unknown')}.",
        f"- Days since last rest day: {ctx.get('days_since_rest', 'unknown')}.",
        f"- Last 7 days: {load_7d.get('session_count', 0)} sessions, "
        f"{load_7d.get('total_duration_min', 0):.0f} min, "
        f"{load_7d.get('total_distance_km', 0):.1f} km. By type: {load_7d.get('by_type', {})}.",
        f"- Last 28 days: {load_28d.get('session_count', 0)} sessions, "
        f"{load_28d.get('total_duration_min', 0):.0f} min.",
    ]
    if sleep.get("avg_sleep_h") is not None:
        lines.append(
            f"- Sleep, last 7 nights: avg {sleep['avg_sleep_h']:.1f}h "
            f"(28-day baseline {sleep.get('baseline_avg_sleep_h', 'n/a')}h, "
            f"{sleep.get('delta_pct', 'n/a')}% delta), {sleep.get('nights_logged', 0)} nights logged."
        )
    else:
        lines.append("- Sleep: no data logged for the last 7 nights.")
    if journal:
        lines.append("- Recent journal entries (newest first):")
        lines.extend(f"  - {e['date']}: {e['text']}" for e in journal)
    else:
        lines.append("- No journal entries in the last 14 days.")
    lines.extend(_sailing_focus_briefing(ctx.get("sailing_focus") or {}))
    lines.extend(_gym_briefing(ctx.get("gym") or {}))
    if ctx.get("calendar"):
        lines.extend(_calendar_briefing(ctx["calendar"]))
    if ctx.get("wind_briefing"):
        lines.append(ctx["wind_briefing"])
    return "\n".join(lines)


SYSTEM_PROMPT = """You are a serious, no-nonsense training coach for an athlete who \
does windsurfing/foiling plus cross-training (cycling, strength, running). You have \
live access to their real training data and wind forecasts: their home spot (Kaunas \
reservoir, a lake) in 3-hour blocks for the next 5 days, and the spot they've most \
recently been sailing when that's somewhere else -- use them, don't ask them to repeat \
anything back to you. Factor the forecast into any advice about an on-water session: say \
plainly if conditions look too light, too gusty, or fine for what they want to do, and \
when planning their week at home name the days and time blocks that suit windfoiling \
(their windfoil sessions start at about 6 kn mean wind; big gusts and direction swings mean \
a shifty lake day).

Form your own independent judgment and say what you actually think, even when it \
contradicts what the athlete proposes -- you are not here to agree by default. If \
their plan risks overtraining, ignores poor sleep or recovery signals, or the wind \
doesn't support the session they want, tell them directly and say why, citing the \
specific numbers. Praise is earned by what the data shows, not handed out to be \
agreeable, and a bad idea stays a bad idea even if they push back on you -- hold \
your position unless they give you new information that actually changes the \
picture. Be concise (a few sentences unless they ask for detail), but don't soften \
a hard call just to be nice. Never invent numbers that aren't in the data below or \
in a tool result.

The data below is only a summary. When the athlete asks about a specific session \
(e.g. "analyse yesterday's windsurf", "how did my last gym session go"), use \
find_sessions to locate it and get_session_details to read it before answering. \
Don't call the tools for general questions the summary already answers. If an \
earlier reply in this conversation said session-level data isn't available, that \
was before you had these tools -- look the session up instead of repeating it. \
For windfoil/windsurf sessions, get_session_details includes a GPS analysis (tacks, \
VMG, port vs starboard, baseline); when the athlete asks about individual tacks or \
gybes, legs, specific moments or consistency, also call get_session_track_detail and \
quote the specific maneuvers or legs (by local time) that support your point.

The athlete keeps a calendar of planned trainings, races, plans and days they \
CAN'T train (exams, meeting friends, parties, other). Treat it as hard constraints \
and build every recommendation around it: never schedule a session on a can't-train \
day, move key sessions to the free days around them, and expect exam periods to cost \
sleep and focus (lighter load, not zero). Plan a taper and a fresh body before a \
race, and don't put a hard day the day before one. After a party, assume poor \
recovery the next day. If a planned training in the past has no session logged, \
point it out. Races further out are listed too; for anything else beyond the four weeks \
shown, call find_calendar_events. \
The calendar is what the athlete intends; Garmin sessions are what actually happened.

When you plan the athlete's week or an on-water session, build the sailing sessions around \
the "sailing focus" points below (from the per-session iQFOIL analyses and the weekly \
summary): turn them into concrete session goals, and match them to the wind forecast.

The athlete logs gym workouts set by set in the gym tracker (the coach's 8-week \
plan, days A-D). The summary below lists the latest ones; for the full sets of a \
workout call find_gym_workouts / get_gym_workout, and for the plan itself -- \
exercises and targets per day, plus the cardio sessions, the athlete's own HR \
zones and the nutrition targets -- call get_gym_plan. Use those HR zones for any \
HR advice. A gym workout may also appear as a Garmin strength_training session \
(same time); the tracker has the real sets, Garmin has the heart rate.

You can also write to the calendar with add_calendar_event, update_calendar_event \
and delete_calendar_event (entries are referenced by their [#id] -- internal, never \
show ids to the athlete). Only write when \
the athlete asks you to (put / add / save / move / reschedule / remove something) or \
says yes to a plan you offered to save -- never on your own initiative. If they ask \
you to plan something without saying to save it, give the plan and offer to add it. \
Never place a training on a can't-train day, and don't change or delete can't-train \
entries or races unless explicitly asked. Put the session structure and targets in \
notes, keep titles short. After writing, say in one line what you changed -- the app \
also shows the athlete the exact changes with an undo button.

Today is {today}.

Athlete's current data:
{briefing}
"""


MAX_TOOL_ROUNDS = 4

# Messages that are clearly about particular sessions. For these the first
# DeepSeek round must call a tool: left to choose, the model sometimes answers
# from the aggregate summary (or from its own earlier "I don't have that data"
# replies in the history) instead of looking the session up.
SESSION_QUERY_RE = re.compile(
    r"analy[sz]|session|workout|activit|yesterday|\blast (ride|run|sail|gym|windsurf|foil|kite)"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[-./]\d{1,2}\b",
    re.IGNORECASE,
)


def _deepseek_response(
    query: str,
    context: dict,
    history: list[dict],
    conn: sqlite3.Connection | None,
    changes: list | None = None,
) -> str | None:
    if not DEEPSEEK_API_KEY:
        return None
    system = SYSTEM_PROMPT.format(
        briefing=_context_briefing(context), today=date.today().strftime("%A %Y-%m-%d")
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(history[-20:])
    messages.append({"role": "user", "content": query})
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            body = {"model": DEEPSEEK_MODEL, "messages": messages, "temperature": 0.6}
            # Offer the tools until the last round, which forces a plain answer.
            if conn is not None and round_no < MAX_TOOL_ROUNDS:
                body["tools"] = TOOL_SPECS
                if round_no == 0 and SESSION_QUERY_RE.search(query):
                    body["tool_choice"] = "required"
            resp = httpx.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json=body,
                timeout=60,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return (msg.get("content") or "").strip() or None
            messages.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
            )
            for call in tool_calls:
                fn = call["function"]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": run_tool(conn, fn["name"], fn.get("arguments"), changes),
                    }
                )
        return None
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        logger.exception("DeepSeek call failed, falling back to templates")
        return None


def generate_response(
    context: dict,
    intent: str,
    query: str = "",
    history: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
    changes: list | None = None,
) -> str:
    """changes, when given, collects every calendar edit the coach makes while
    answering; without it the calendar write tools refuse."""
    deepseek_reply = _deepseek_response(query, context, history or [], conn, changes)
    if deepseek_reply:
        return deepseek_reply
    return RESPONSE_BUILDERS.get(intent, _help)(context)
