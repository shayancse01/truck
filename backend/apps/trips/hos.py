"""FMCSA Hours-of-Service schedule engine for property-carrying drivers.

Implements the 70-hour / 8-day rolling duty cycle (49 CFR Part 395):
  * 11-hour driving limit per duty day (resets after 10 consecutive hours off)
  * 14-hour driving window (starts with the first duty event after >=10h off;
    every hour counts against it, on- or off-duty)
  * 30-minute break from driving required no later than every 8 cumulative
    driving hours (any non-driving status qualifies if consecutive)
  * 70-hour on-duty cap over a rolling 8-day window; the oldest calendar
    day's duty hours drop off at local midnight
  * Optional 34-hour restart zeroes the 70-hour clock (§395.3(c))
Assumptions from the brief: fuel at least once every 1,000 miles, 1 hour each
for pickup and drop-off, no adverse driving conditions.

The simulation advances in 5-minute ticks. All times are naive local time of
the driver's departure zone (UTC offset supplied by the caller).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

TICK_MIN = 5
SEG_HOURS = TICK_MIN / 60.0

# Statuses used both internally and on the rendered grid-graph ELD logs.
OFF = "off"    # off-duty
SB = "sb"      # sleeper-berth rest
ON = "on"      # on-duty not driving (pretrip, fueling, pickup/drop-off paperwork)
D = "drive"    # driving


@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    status: str
    note: str = ""
    location: str = ""
    odometer_end: float | None = None  # cumulative miles at segment end (driving only)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def prev_miles(events: list[Event], i: int) -> float:
    for j in range(i - 1, -1, -1):
        if events[j].odometer_end is not None:
            return events[j].odometer_end
    return 0.0


def grid_rows(events: list[Event]) -> list[dict]:
    """Convert events into horizontal segments (hours 0..24) for the log grid."""
    rows = []
    for e in events:
        s = e.start.hour + e.start.minute / 60
        en = (e.end.hour + e.end.minute / 60) if e.end.date() == e.start.date() else 24.0
        if en <= s:  # segment ends exactly at midnight
            en = 24.0
        rows.append({
            "status": e.status,
            "start": round(s, 3),
            "end": round(en, 3),
            "note": e.note,
            "start_label": e.start.strftime("%H:%M"),
            "end_label": ("24:00" if en == 24.0 else e.end.strftime("%H:%M")),
        })
    return rows


@dataclass
class DayLog:
    date: dt.date
    events: list[Event] = field(default_factory=list)
    drive_hours: float = 0.0
    on_hours: float = 0.0
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    cycle_used_at_start: float = 0.0
    cycle_left_at_end: float = 0.0

    def to_dict(self) -> dict:
        miles = 0.0
        for i, e in enumerate(self.events):
            if e.status == D and e.odometer_end is not None:
                miles += e.odometer_end - prev_miles(self.events, i)
        return {
            "date": self.date.isoformat(),
            "events": [
                {
                    "status": e.status,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "hours": round(e.hours, 2),
                    "note": e.note,
                    "location": e.location,
                    "odometer_end": round(e.odometer_end, 1) if e.odometer_end is not None else None,
                }
                for e in self.events
            ],
            "grid_rows": grid_rows(self.events),
            "totals": {
                "drive_hours": round(self.drive_hours, 2),
                "on_hours": round(self.on_hours, 2),
                "miles": int(round(miles)),
                "window_start": self.window_start.isoformat() if self.window_start else None,
                "window_end": self.window_end.isoformat() if self.window_end else None,
                "cycle_used_at_start": round(self.cycle_used_at_start, 2),
                "cycle_left_at_end": round(self.cycle_left_at_end, 2),
            },
        }


class ScheduleError(Exception):
    pass


def nearest_place(mile: float, total: float) -> str:
    frac = mile / total if total else 0
    if frac <= 0.02:
        return "Origin"
    if frac >= 0.985:
        return "Destination"
    return f"Mile marker {int(round(mile))}"


def generate_schedule(
    *,
    distance_miles: float,
    drive_hours_total: float,
    cycle_used_hours: float,
    start_time: dt.datetime,
    avg_speed_mph: float | None = None,
    allow_34hr_restart: bool = True,
) -> dict:
    """Simulate the trip tick-by-tick under HOS rules and emit an ELD plan."""
    if distance_miles <= 0 or drive_hours_total <= 0:
        raise ScheduleError("Route must have positive distance and duration.")
    if not (0 <= cycle_used_hours < 70):
        raise ScheduleError("Current cycle used must be between 0 and 70 hours.")

    speed = avg_speed_mph or (distance_miles / max(drive_hours_total, 1e-6))
    speed = max(35.0, min(70.0, speed))
    pure_drive_hours = distance_miles / speed

    t = start_time.replace(second=0, microsecond=0)
    odometer = 0.0
    events: list[Event] = []
    stops: list[dict] = []

    # ---- state ---------------------------------------------------------------
    cycle_used = float(cycle_used_hours)           # rolling 8-day on-duty total
    duty_by_day: dict[dt.date, float] = {}
    if cycle_used > 0:
        # Fold the driver's history into a synthetic recent day so that it
        # rolls out of the window naturally over the coming days.
        duty_by_day[t.date() - dt.timedelta(days=1)] = cycle_used

    drive_window = 0.0          # driving hours since last >=10h rest (NOT calendar)
    window_elapsed = 0.0        # elapsed hours of the open 14-hour window
    duty_since_window = 0.0     # on-duty time accrued inside the open window
    window_open = False
    window_start: dt.datetime | None = None
    duty_today = 0.0            # on-duty hours booked to the CURRENT calendar day
    last_duty_end: dt.datetime | None = None
    day_window_bounds: dict = {}  # date -> (window_start, window_end) for log headers
    drive_since_break = 0.0
    next_fuel_due = 1000.0
    pending_note = "Start of duty — pickup / pre-trip inspection (1 hr)"
    completed = False

    def credit_duty(now: dt.datetime, hours: float) -> None:
        d = now.date()
        duty_by_day[d] = duty_by_day.get(d, 0.0) + hours

    def roll_off(now: dt.datetime) -> None:
        """Oldest calendar day drops out of the rolling 8-day window at midnight."""
        nonlocal cycle_used
        oldest = now.date() - dt.timedelta(days=8)
        if oldest in duty_by_day:
            cycle_used = max(0.0, cycle_used - duty_by_day.pop(oldest))

    def take_rest(now: dt.datetime, hours: float, label: str, restart: bool = False) -> dt.datetime:
        nonlocal drive_window, window_elapsed, window_open, drive_since_break, \
            cycle_used, duty_since_window, window_start
        end = now + dt.timedelta(hours=hours)
        loc = nearest_place(odometer, distance_miles)
        st = SB if hours >= 10.0 - 1e-9 else OFF
        events.append(Event(start=now, end=end, status=st, note=label, location=loc))
        stops.append({"type": "rest", "mile": round(odometer), "time": now.isoformat(),
                      "end": end.isoformat(), "hours": hours, "label": label, "location": loc})
        if hours >= 10.0 - 1e-9:  # a full rest resets the 11/14-hour clocks
            drive_window = 0.0
            window_elapsed = 0.0
            duty_since_window = 0.0
            window_open = False
            window_start = None
            drive_since_break = 0.0
        else:
            window_elapsed += hours  # short breaks still tick the 14-hour window
        if restart:
            cycle_used = 0.0
            duty_by_day.clear()
        return end

    duty_day_of: dt.date | None = None
    t_now = [t]  # single mutable clock used by every helper below
    deadline = t + dt.timedelta(days=45)

    while odometer < distance_miles and t_now[0] < deadline:
        t = t_now[0]
        roll_off(t)

        # Calendar-day bookkeeping: a fresh duty day begins after any rest
        # long enough to reset the 11/14 clocks.
        if (not window_open and (last_duty_end is None
                                 or (t - last_duty_end) >= dt.timedelta(hours=10))) \
                or t.date() != duty_day_of:
            duty_today = 0.0
            duty_day_of = t.date()

        h_now = t.hour + t.minute / 60.0
        # Midnight rule: no NEW window may start so late that its duty would
        # spill into tomorrow with nothing left to show for it today.
        late_start_ok = (not window_open) and (duty_today < 1.0) and (h_now > 22.0)
        can_drive = (
            drive_window < 11.0 - 1e-9
            and (not window_open or window_elapsed < 14.0 - 1e-9)
            and duty_today < 14.0 - 1e-9                        # per-duty-day cap
            and (not window_open                                # no new window after 22:00
                 or h_now < 22.0)
            and cycle_used < 70.0 - 1e-9
            and drive_since_break < 8.0 - 1e-9
        )

        if not can_drive:
            if cycle_used >= 70.0 - 1e-9:
                recent = sum(v for k, v in duty_by_day.items()
                             if 0 <= (t.date() - k).days <= 7)
                if allow_34hr_restart and recent >= 50.0 - 1e-9:
                    t_now[0] = take_rest(t, 34.0, "34-hour restart — resets the 70-hour cycle", restart=True)
                else:
                    t_now[0] = take_rest(t, 10.0, "Rest until cycle hours roll off")
                continue
            if window_open and window_elapsed >= 14.0 - 1e-9:
                t_now[0] = take_rest(t, 10.0, "14-hour window closed — 10-hour rest resets the clocks")
                continue
            if window_open:
                t_now[0] = take_rest(t, 10.0, "End of duty day — 10-hour rest resets the clocks")
                continue
            if drive_window >= 11.0 - 1e-9:
                t_now[0] = take_rest(t, 10.0, "11-hour driving limit reached — 10-hour rest")
                continue
            if not window_open and h_now >= 22.0:
                t_now[0] = take_rest(t, max(8.0, 24.0 - h_now + 8.0),
                                     "Night rest — day starts early tomorrow")
                continue
            if drive_since_break >= 8.0 - 1e-9:
                t_now[0] = take_rest(t, 0.5, "Required 30-minute break from driving §395.3(a)(3)(ii)")
                continue
            t_now[0] = take_rest(t, 10.0, "10-hour off-duty rest")
            continue

        # ============================ DRIVE TICK ==============================
        if not window_open:
            window_open = True
            window_elapsed = 0.0
            duty_since_window = 0.0
            window_start = t
            day_window_bounds.setdefault(t.date(), [window_start, None])

        def spend_duty(hours: float, status_: str, note: str, loc: str,
                       is_drive: bool, miles_: float = 0.0) -> None:
            """Append one duty segment and update ALL rule clocks consistently."""
            nonlocal drive_window, window_elapsed, drive_since_break, cycle_used
            nonlocal duty_since_window, odometer, duty_today, last_duty_end
            ev = Event(start=t_now[0], end=t_now[0] + dt.timedelta(hours=hours),
                       status=status_, note=note, location=loc)
            if is_drive:
                ev.odometer_end = odometer + miles_
                odometer += miles_
                drive_window += hours
                drive_since_break += hours
            else:
                drive_since_break = 0.0
            window_elapsed += hours
            duty_since_window += hours
            cycle_used += hours
            duty_today += hours
            credit_duty(t_now[0], hours)
            events.append(ev)
            last_duty_end = t_now[0] + dt.timedelta(hours=hours)
            t_now[0] = t_now[0] + dt.timedelta(hours=hours)
            if window_start is not None:
                b = day_window_bounds.setdefault(t_now[0].date(), [window_start, None])
                b[1] = max(b[1] or t_now[0], t_now[0])
        # First duty event of a fresh window with a pending "start of duty"
        # note: log the mandatory pickup / pre-trip hour as on-duty NOT driving.
        if pending_note and "pickup / pre-trip" in pending_note:
            spend_duty(1.0, ON, "Pickup / pre-trip inspection (1 hr)", "Origin", False)
            pending_note = ""

        remaining = distance_miles - odometer
        seg_hours = min(SEG_HOURS, remaining / speed)
        miles = seg_hours * speed
        new_odometer = odometer + miles


        spend_duty(seg_hours, D, pending_note,
                   nearest_place(new_odometer, distance_miles), True, miles)
        pending_note = ""

        if odometer >= distance_miles - 1e-9:
            # Arrive destination: 1 hour drop-off/unload duty, then final rest.
            spend_duty(1.0, ON, "Arrive destination — drop-off / unload (1 hr)",
                       "Destination", False)
            t_now[0] = take_rest(t_now[0], 10.0,
                                 "Delivery complete — 10-hour rest (plan ends)")
            completed = True
            break

        # Fuel stop: at least once every 1,000 miles (placed before it is due).
        if odometer >= next_fuel_due - 60:
            loc = nearest_place(odometer, distance_miles)
            spend_duty(0.75, ON, f"Fuel stop near {loc} (45 min — also satisfies the 30-min break)",
                       loc, False)
            stops.append({"type": "fuel", "mile": round(odometer), "time": t_now[0].isoformat(),
                          "location": loc})
            next_fuel_due = odometer + 1000.0
            continue

        # 30-minute break from driving after 8 cumulative driving hours.
        if drive_since_break >= 8.0 - 1e-9:
            t_now[0] = take_rest(t_now[0], 0.5,
                                 "Required 30-minute break from driving §395.3(a)(3)(ii)")
            stops.append({"type": "break30", "mile": round(odometer),
                          "time": t_now[0].isoformat(),
                          "location": nearest_place(odometer, distance_miles)})
            continue

    if not completed and odometer < distance_miles - 1e-9:
        raise ScheduleError("Could not legally complete the trip within 45 simulated days.")

    ordered = sorted(events, key=lambda e: e.start)

    # attach per-day window bounds to DayLog construction below

    # ---- daily logs & rolling-cycle display -----------------------------------
    cum_on: dict[dt.date, float] = {}
    for ev in ordered:
        if ev.status in (D, ON):
            cum_on[ev.start.date()] = cum_on.get(ev.start.date(), 0.0) + ev.hours

    day_dates = sorted({e.start.date() for e in ordered})
    rolling: dict[dt.date, float] = {}
    acc = float(cycle_used_hours)
    for d in day_dates:
        prior = sum(v for k, v in cum_on.items() if (d - k).days >= 8)
        rolling[d] = max(0.0, acc - prior)
        acc += cum_on.get(d, 0.0)

    days: dict[dt.date, DayLog] = {}
    for d in day_dates:
        dl = DayLog(date=d)
        b = day_window_bounds.get(d)
        if b and b[0]:
            dl.window_start = b[0]
            dl.window_end = b[1] or b[0]
        days[d] = dl
    for ev in ordered:
        dl = days[ev.start.date()]
        dl.events.append(ev)
        if ev.status == D:
            dl.drive_hours += ev.hours
            dl.on_hours += ev.hours
        elif ev.status == ON:
            dl.on_hours += ev.hours
        if ev.status in (D, ON):
            if dl.window_start is None:
                dl.window_start = ev.start
            dl.window_end = max(dl.window_end or ev.end, ev.end)

    for d, dl in days.items():
        dl.cycle_used_at_start = rolling.get(d, 0.0)
        used_after = rolling.get(d, 0.0) + cum_on.get(d, 0.0)
        dl.cycle_left_at_end = max(0.0, 70.0 - used_after)

    logs = [days[d].to_dict() for d in day_dates]
    finish = ordered[-1].end
    total_on = sum(cum_on.values())

    return {
        "summary": {
            "distance_miles": round(distance_miles),
            "avg_speed_mph": round(speed, 1),
            "pure_drive_hours": round(pure_drive_hours, 2),
            "total_drive_hours": round(sum(e.hours for e in ordered if e.status == D), 2),
            "total_on_duty_hours": round(total_on, 2),
            "total_rest_hours": round(sum(e.hours for e in ordered if e.status in (OFF, SB)), 2),
            "elapsed_hours": round((finish - start_time).total_seconds() / 3600.0, 2),
            "arrival_time": finish.isoformat(),
            "days_required": len(logs),
            "starting_cycle_used": round(cycle_used_hours, 2),
            "ending_cycle_used": round(min(70.0, cycle_used_hours + total_on), 2),
            "num_rest_stops": len([s for s in stops if s["type"] == "rest"]),
            "num_fuel_stops": len([s for s in stops if s["type"] == "fuel"]),
            "used_34hr_restart": any("34-hour" in (s.get("label") or "") for s in stops),
        },
        "timeline": [
            {
                "status": e.status,
                "start": e.start.isoformat(),
                "end": e.end.isoformat(),
                "hours": round(e.hours, 2),
                "note": e.note,
                "location": e.location,
                "miles_at": round(e.odometer_end, 1) if e.odometer_end is not None else None,
            }
            for e in ordered
        ],
        "stops": stops,
        "daily_logs": logs,
    }
