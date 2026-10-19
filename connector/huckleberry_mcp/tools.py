"""MCP tools. Each call uses the Firebase ID token carried in the caller's access token."""

from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from google.api_core.exceptions import GoogleAPICallError, PermissionDenied, Unauthenticated
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from huckleberry_api import HuckleberryAPI
from huckleberry_api.firebase_types import (
    ActivityMode,
    BottleType,
    DiaperMode,
    FirebaseSleepDetails,
    FirebaseSleepLocations,
    MedicationUnits,
    PooColor,
    PooConsistency,
    PottyResult,
    SolidsReaction,
    TemperatureUnits,
    VolumeUnits,
)
from huckleberry_api.models import SolidsFoodReference

from . import firebase_auth
from .timeutil import parse_time, show_time

INSTRUCTIONS = """\
Reads and logs the signed-in parent's Huckleberry baby tracking data: sleep, feeds (nursing,
bottle, solids), diapers and potty, pumping, growth, temperature, medicine, and activities,
including the app's live timers. Parents often use this one-handed or at night: be brief and
state the outcome plainly ("Sleep timer started for Chloe at 7:42 PM").

- Before a write, if you inferred anything the user didn't say (which child, the time, pee vs.
  poo, amounts or units), confirm it with them first.
- Never repeat a write that failed or timed out without first checking with get_current_status
  or get_entries, so nothing is logged twice. There is no delete tool: entries logged by mistake
  must be removed in the Huckleberry app.
- To log medicine, use a medicine name from list_medicines. To log solids, use foods from
  find_solid_foods, passing each food's id, source, and name exactly as returned.
- Times: prefer ISO 8601 with the user's UTC offset. "now", "15m", "2h", and "HH:MM" are also
  accepted and read in timezone_name, which defaults to the time zone the user chose when
  connecting. Pass timezone_name only if they say they're somewhere else.
"""

mcp = MCPServer(name="Huckleberry", instructions=INSTRUCTIONS)
DEFAULT_TIMEZONE: str = "America/New_York"  # replaced from Settings in app.py; used when a token has no time zone

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
TIMER = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
LOG = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DISCARD = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)

Tracker = Literal["sleep", "feeding", "diaper", "health", "pump", "activity"]
TimerTracker = Literal["sleep", "nursing", "pump", "activity"]
Amount = Literal["little", "medium", "big"]
# The flags of FirebaseSleepLocations, as the app stores them
SleepLocation = Literal[
    "car", "nursing", "wornOrHeld", "stroller", "coSleep", "nextToCarer", "onOwnInBed", "bottle", "swing"
]

T = TypeVar("T")


# --- Plumbing -------------------------------------------------------------------------------


def _tz(name: Optional[str]) -> ZoneInfo:
    """The named zone, else the one the user chose when signing in, else the server default."""
    name = name or getattr(get_access_token(), "timezone", None) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        raise ToolError(f"Unknown timezone '{name}'; use an IANA name like America/Chicago") from None


def _time(value: Optional[str], tz: ZoneInfo) -> datetime:
    try:
        return parse_time(value, tz)
    except ValueError as e:
        raise ToolError(str(e)) from None


@asynccontextmanager
async def _api(tz: ZoneInfo) -> AsyncGenerator[HuckleberryAPI]:
    token = get_access_token()
    id_token = getattr(token, "id_token", None)
    uid = getattr(token, "uid", None)
    expires_at = getattr(token, "id_token_expires_at", None)
    if not (token and id_token and uid and expires_at):
        raise ToolError("Not signed in; reconnect the Huckleberry connector")
    try:
        async with firebase_auth.api_client(token.subject or "", id_token, uid, expires_at, str(tz)) as api:
            yield api
    except firebase_auth.AuthError:
        raise ToolError("Huckleberry sign-in expired; reconnect the connector to sign in again") from None


async def _call(awaitable: Awaitable[T]) -> T:
    """Await a library call, turning its errors into tool errors the model can relay."""
    try:
        return await awaitable
    except Unauthenticated, PermissionDenied:
        raise ToolError("Huckleberry rejected the sign-in; reconnect the connector to sign in again") from None
    except GoogleAPICallError as e:
        raise ToolError(f"Huckleberry error: {e.message}") from None
    except aiohttp.ClientResponseError as e:
        if e.status in (401, 403):
            raise ToolError("Huckleberry rejected the sign-in; reconnect the connector to sign in again") from None
        raise ToolError(f"Huckleberry error: {e.message}") from None
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ToolError(f"Couldn't reach Huckleberry: {e or type(e).__name__}") from None
    except ValidationError as e:
        raise ToolError(f"Huckleberry returned data this server doesn't understand: {e.error_count()} errors") from None
    except (ValueError, RuntimeError) as e:
        raise ToolError(str(e)) from None


async def _children(api: HuckleberryAPI) -> List[Dict[str, Any]]:
    user = await _call(api.get_user())
    if not user or not user.childList:
        raise ToolError("No children found on this Huckleberry account")
    children = []
    for ref in user.childList:
        doc = await _call(api.get_child(ref.cid))
        children.append(
            {
                "id": ref.cid,
                "name": (doc.childsName if doc else None) or ref.nickname or ref.cid,
                "birthdate": doc.birthdate if doc else None,
            }
        )
    return children


async def _child(api: HuckleberryAPI, child: Optional[str]) -> Dict[str, Any]:
    """The child matching a name or id; the only child when there's one and none is named."""
    children = await _children(api)
    if child:
        wanted = child.strip().lower()
        matches = [c for c in children if wanted in (c["id"].lower(), str(c["name"]).lower())]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(str(c["name"]) for c in children)
        raise ToolError(f"No single child matches '{child}'; children on this account: {names}")
    if len(children) == 1:
        return children[0]
    raise ToolError(f"Several children on this account ({', '.join(str(c['name']) for c in children)}); pass child")


def _localize(value: Any, tz: ZoneInfo) -> Any:
    """Dump models, and next to every start/duration pair (seconds) add readable local times."""
    if isinstance(value, BaseModel):
        value = value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        out = {k: _localize(v, tz) for k, v in value.items()}
        start = out.get("start")
        if isinstance(start, (int, float)) and not isinstance(start, bool) and start > 0:
            out["start_local"] = show_time(start, tz)
            duration = out.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
                out["end_local"] = show_time(start + duration, tz)
        return out
    if isinstance(value, list):
        return [_localize(v, tz) for v in value]
    return value


def _ms(value: Optional[float], tz: ZoneInfo) -> Optional[str]:
    return show_time(value / 1000, tz) if value else None


def _minutes(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 60, 3)


# --- Status ---------------------------------------------------------------------------------


async def _status(api: HuckleberryAPI, child_id: str, tz: ZoneInfo, only: Optional[TimerTracker] = None):
    """The live timers (and last-entry summaries) for one child."""
    status: Dict[str, Any] = {}

    if only in (None, "sleep"):
        sleep = await _call(api.get_sleep(child_id))
        timer = sleep.timer if sleep else None
        status["sleep_timer"] = (
            {"running": True, "paused": timer.paused, "started": _ms(timer.timerStartTime, tz)}
            if timer and timer.active
            else {"running": False}
        )
        if only is None and sleep and sleep.prefs and sleep.prefs.lastSleep:
            status["last_sleep"] = _localize(sleep.prefs.lastSleep, tz)

    if only in (None, "nursing"):
        feed = await _call(api.get_nursing(child_id))
        timer = feed.timer if feed else None
        status["nursing_timer"] = (
            {
                "running": True,
                "paused": timer.paused,
                "active_side": timer.activeSide,
                "started": show_time(timer.feedStartTime, tz) if timer.feedStartTime else None,
                "left_seconds_before_current_side": timer.leftDuration,
                "right_seconds_before_current_side": timer.rightDuration,
            }
            if timer and timer.active
            else {"running": False}
        )
        if only is None and feed and feed.prefs:
            for key in ("lastNursing", "lastBottle", "lastSolid"):
                if getattr(feed.prefs, key):
                    status[key] = _localize(getattr(feed.prefs, key), tz)

    if only in (None, "pump"):
        pump = await _call(api.get_pump(child_id))
        timer = pump.timer if pump else None
        status["pump_timer"] = (
            {"running": True, "paused": bool(timer.paused), "started": _ms(timer.startTime, tz)}
            if timer and timer.active
            else {"running": False}
        )
        if only is None and pump and pump.prefs and pump.prefs.lastPump:
            status["last_pump"] = _localize(pump.prefs.lastPump, tz)

    if only in (None, "activity"):
        activities = await _call(api.get_activities(child_id))
        running = {}
        if activities and activities.timer:
            for mode, entry in activities.timer:
                if entry is not None and entry.active:
                    running[mode] = {
                        "paused": bool(entry.paused),
                        "started": _ms(entry.startTime, tz),
                        "seconds_when_paused": entry.duration if entry.paused else None,
                    }
        status["activity_timers"] = running

    if only is None:
        diaper = await _call(api.get_diaper(child_id))
        if diaper and diaper.prefs:
            if diaper.prefs.lastDiaper:
                status["last_diaper"] = _localize(diaper.prefs.lastDiaper, tz)
            if diaper.prefs.lastPotty:
                status["last_potty"] = _localize(diaper.prefs.lastPotty, tz)
    return status


@mcp.tool(annotations=READ)
async def list_children() -> List[Dict[str, Any]]:
    """List the children on the account with their id, name, and birthdate."""
    async with _api(_tz(None)) as api:
        return await _children(api)


@mcp.tool(annotations=READ)
async def get_current_status(child: Optional[str] = None, timezone_name: Optional[str] = None) -> Dict[str, Any]:
    """What's happening now: running sleep, nursing, pump, and activity timers, plus the last
    sleep, feeds, pump, diaper, and potty entries. Durations are in seconds.

    child: name or id; only needed if the account has more than one child.
    """
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        return {"child": kid["name"], **await _status(api, kid["id"], tz)}


@mcp.tool(annotations=READ)
async def get_entries(
    tracker: Tracker,
    hours: int = 24,
    limit: int = 50,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Logged entries from one tracker in the last `hours` hours, newest first, as Huckleberry stores
    them, with start_local/end_local added. start is epoch seconds, durations are seconds, and
    offset is the UTC offset in minutes (negated). feeding covers nursing (mode breast, per-side
    durations), bottle, and solids; health covers growth, medication, and temperature."""
    if not 1 <= hours <= 24 * 92:
        raise ToolError("hours must be between 1 and 2208 (92 days)")
    tz = _tz(timezone_name)
    end = datetime.now(tz)
    start = end - timedelta(hours=hours)
    async with _api(tz) as api:
        kid = await _child(api, child)
        fetch = {
            "sleep": api.list_sleep_intervals,
            "feeding": api.list_feed_intervals,
            "diaper": api.list_diaper_intervals,
            "health": api.list_health_entries,
            "pump": api.list_pump_intervals,
            "activity": api.list_activity_intervals,
        }[tracker]
        entries = await _call(fetch(kid["id"], start, end))
    entries.sort(key=lambda e: float(e.start), reverse=True)
    shown = entries[: max(1, limit)]
    return {"child": kid["name"], "count": len(entries), "entries": _localize(shown, tz)}


@mcp.tool(annotations=READ)
async def list_medicines(child: Optional[str] = None) -> List[Dict[str, Any]]:
    """The medicines set up for a child (name, id, and the last amount/units given), for log_medicine."""
    async with _api(_tz(None)) as api:
        kid = await _child(api, child)
        types = await _call(api.list_medicine_types(kid["id"]))
    return [t.model_dump(by_alias=True, exclude_none=True) for t in types if t.active]


@mcp.tool(annotations=READ)
async def find_solid_foods(query: str = "", limit: int = 20, child: Optional[str] = None) -> Dict[str, Any]:
    """Foods for log_solids: the child's custom foods and Huckleberry's curated food list,
    filtered by a case-insensitive name match on `query` (empty lists the most common)."""
    wanted = query.strip().lower()
    async with _api(_tz(None)) as api:
        kid = await _child(api, child)
        custom = await _call(api.list_solids_custom_foods(kid["id"]))
        curated = await _call(api.list_solids_curated_foods())

    def matches(name: str, aka: Optional[List[str]] = None) -> bool:
        return not wanted or any(wanted in n.lower() for n in [name, *(aka or [])])

    found: List[Dict[str, Any]] = [{"id": f.id, "source": f.source, "name": f.name} for f in custom if matches(f.name)]
    found += [
        {
            "id": f.id,
            "source": f.source,
            "name": f.name,
            "common_allergen": f.is_common_allergen,
            "high_choking_hazard": f.is_high_choking_hazard,
        }
        for f in curated
        if matches(f.name, f.aka)
    ]
    return {"count": len(found), "foods": found[: max(1, limit)]}


# --- Timers ---------------------------------------------------------------------------------


@mcp.tool(annotations=TIMER)
async def sleep_timer(
    action: Literal["start", "pause", "resume", "complete"],
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Control the live sleep timer, like the app's sleep button. complete saves the sleep to history.
    Returns the timer's new state."""
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        action_fn = {
            "start": api.start_sleep,
            "pause": api.pause_sleep,
            "resume": api.resume_sleep,
            "complete": api.complete_sleep,
        }[action]
        await _call(action_fn(kid["id"]))
        return {"child": kid["name"], "done": action, **await _status(api, kid["id"], tz, "sleep")}


@mcp.tool(annotations=TIMER)
async def nursing_timer(
    action: Literal["start", "pause", "resume", "switch_side", "complete"],
    side: Optional[Literal["left", "right"]] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Control the live breastfeeding timer. side is for start (default left) and resume (default:
    the side it was paused on). complete saves the feed to history. Returns the timer's new state."""
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        if action == "start":
            await _call(api.start_nursing(kid["id"], side=side or "left"))
        elif action == "resume":
            await _call(api.resume_nursing(kid["id"], side=side))
        else:
            action_fn = {
                "pause": api.pause_nursing,
                "switch_side": api.switch_nursing_side,
                "complete": api.complete_nursing,
            }[action]
            await _call(action_fn(kid["id"]))
        return {"child": kid["name"], "done": action, **await _status(api, kid["id"], tz, "nursing")}


@mcp.tool(annotations=TIMER)
async def pump_timer(
    action: Literal["start", "pause", "resume", "complete"],
    left_amount: Optional[float] = None,
    right_amount: Optional[float] = None,
    total_amount: Optional[float] = None,
    units: Optional[VolumeUnits] = None,
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Control the live pumping timer. complete saves the session and needs either total_amount, or
    both left_amount and right_amount (units default to the timer's or ml). Returns the new state."""
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        if action == "complete":
            await _call(
                api.complete_pump(
                    kid["id"],
                    left_amount=left_amount,
                    right_amount=right_amount,
                    total_amount=total_amount,
                    units=units,
                    notes=notes,
                )
            )
        else:
            action_fn = {"start": api.start_pump, "pause": api.pause_pump, "resume": api.resume_pump}[action]
            await _call(action_fn(kid["id"]))
        return {"child": kid["name"], "done": action, **await _status(api, kid["id"], tz, "pump")}


@mcp.tool(annotations=TIMER)
async def activity_timer(
    activity: ActivityMode,
    action: Literal["start", "pause", "resume", "complete", "switch"],
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Control a live activity timer (bath, tummy time, ...). switch moves the currently running
    activity timer over to `activity`. notes applies to complete. Returns the running timers."""
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        if action == "complete":
            await _call(api.complete_activity(kid["id"], activity, notes=notes))
        else:
            action_fn = {
                "start": api.start_activity,
                "pause": api.pause_activity,
                "resume": api.resume_activity,
                "switch": api.switch_activity,
            }[action]
            await _call(action_fn(kid["id"], activity))
        return {"child": kid["name"], "done": action, **await _status(api, kid["id"], tz, "activity")}


@mcp.tool(annotations=DISCARD)
async def cancel_timer(
    tracker: TimerTracker,
    activity: Optional[ActivityMode] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Discard a running timer without saving anything to history. activity is required for the
    activity tracker."""
    tz = _tz(timezone_name)
    async with _api(tz) as api:
        kid = await _child(api, child)
        if tracker == "activity":
            if not activity:
                raise ToolError("Pass the activity whose timer to cancel")
            await _call(api.cancel_activity(kid["id"], activity))
        else:
            action_fn = {"sleep": api.cancel_sleep, "nursing": api.cancel_nursing, "pump": api.cancel_pump}[tracker]
            await _call(action_fn(kid["id"]))
        return {"child": kid["name"], "cancelled": tracker, **await _status(api, kid["id"], tz, tracker)}


# --- Logging past entries -------------------------------------------------------------------


def _logged(kind: str, kid: Dict[str, Any], at: datetime, tz: ZoneInfo, **details: Any) -> Dict[str, Any]:
    return {"logged": kind, "child": kid["name"], "at": show_time(at, tz), **{k: v for k, v in details.items() if v}}


@mcp.tool(annotations=LOG)
async def log_sleep(
    start_time: str,
    end_time: str,
    locations: Optional[List[SleepLocation]] = None,
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a finished sleep. Times: ISO 8601 with offset, or now, 15m, 2h, HH:MM in timezone_name.
    locations are the app's "where" tags (e.g. onOwnInBed, stroller, car, wornOrHeld)."""
    tz = _tz(timezone_name)
    start, end = _time(start_time, tz), _time(end_time, tz)
    if end <= start:
        raise ToolError("end_time must be after start_time")
    if end - start > timedelta(hours=24):
        raise ToolError("That's over 24 hours; check the times")
    details = None
    if locations or notes:
        details = FirebaseSleepDetails(
            sleepLocations=FirebaseSleepLocations(**{loc: True for loc in locations}) if locations else None,
            notes=notes,
        )
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(api.log_sleep(kid["id"], start_time=start, end_time=end, details=details))
    return _logged("sleep", kid, start, tz, until=show_time(end, tz), minutes=int((end - start).total_seconds() // 60))


@mcp.tool(annotations=LOG)
async def log_nursing(
    start_time: str,
    end_time: str,
    last_side: Literal["left", "right"] = "left",
    left_minutes: Optional[float] = None,
    right_minutes: Optional[float] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a finished breastfeed. Give both left_minutes and right_minutes, or neither to count the
    whole time on last_side."""
    tz = _tz(timezone_name)
    start, end = _time(start_time, tz), _time(end_time, tz)
    if end <= start:
        raise ToolError("end_time must be after start_time")
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(
            api.log_nursing(
                kid["id"],
                start_time=start,
                end_time=end,
                side=last_side,
                left_duration=_minutes(left_minutes),
                right_duration=_minutes(right_minutes),
            )
        )
    return _logged("nursing", kid, start, tz, until=show_time(end, tz), last_side=last_side)


@mcp.tool(annotations=LOG)
async def log_bottle(
    amount: float,
    units: VolumeUnits,
    bottle_type: BottleType = "Formula",
    time: str = "now",
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a bottle feed."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(api.log_bottle(kid["id"], start_time=at, amount=amount, bottle_type=bottle_type, units=units))
    return _logged("bottle", kid, at, tz, amount=f"{amount:g} {units}", bottle_type=bottle_type)


@mcp.tool(annotations=LOG)
async def log_solids(
    foods: List[SolidsFoodReference],
    reaction: Optional[SolidsReaction] = None,
    notes: str = "",
    time: str = "now",
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a solids meal. Each food needs id, source, and name exactly as find_solid_foods returned
    them, plus an amount (the app uses words like "small" or "medium")."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(api.log_solids(kid["id"], start_time=at, foods=foods, notes=notes, reaction=reaction))
    return _logged("solids", kid, at, tz, foods=[f.name for f in foods], reaction=reaction)


@mcp.tool(annotations=LOG)
async def log_diaper(
    mode: DiaperMode,
    time: str = "now",
    pee_amount: Optional[Amount] = None,
    poo_amount: Optional[Amount] = None,
    color: Optional[PooColor] = None,
    consistency: Optional[PooConsistency] = None,
    diaper_rash: bool = False,
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a diaper change. mode: pee, poo, both, or dry. color and consistency describe poo."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(
            api.log_diaper(
                kid["id"],
                start_time=at,
                mode=mode,
                pee_amount=pee_amount,
                poo_amount=poo_amount,
                color=color,
                consistency=consistency,
                diaper_rash=diaper_rash,
                notes=notes,
            )
        )
    return _logged("diaper", kid, at, tz, mode=mode)


@mcp.tool(annotations=LOG)
async def log_potty(
    mode: DiaperMode,
    how_it_happened: PottyResult,
    time: str = "now",
    pee_amount: Optional[Amount] = None,
    poo_amount: Optional[Amount] = None,
    color: Optional[PooColor] = None,
    consistency: Optional[PooConsistency] = None,
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a potty-training event. how_it_happened: wentPotty, accident, or satButDry."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(
            api.log_potty(
                kid["id"],
                start_time=at,
                mode=mode,
                how_it_happened=how_it_happened,
                pee_amount=pee_amount,
                poo_amount=poo_amount,
                color=color,
                consistency=consistency,
                notes=notes,
            )
        )
    return _logged("potty", kid, at, tz, mode=mode, how_it_happened=how_it_happened)


@mcp.tool(annotations=LOG)
async def log_pump(
    time: str = "now",
    duration_minutes: Optional[float] = None,
    left_amount: Optional[float] = None,
    right_amount: Optional[float] = None,
    total_amount: Optional[float] = None,
    units: VolumeUnits = "ml",
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a finished pumping session (time is when it started). Give total_amount, or both
    left_amount and right_amount."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(
            api.log_pump(
                kid["id"],
                start_time=at,
                duration=_minutes(duration_minutes),
                left_amount=left_amount,
                right_amount=right_amount,
                total_amount=total_amount,
                units=units,
                notes=notes,
            )
        )
    amounts = (
        f"{total_amount:g} {units}" if total_amount is not None else f"L {left_amount:g} / R {right_amount:g} {units}"
    )
    return _logged("pump", kid, at, tz, amount=amounts)


@mcp.tool(annotations=LOG)
async def log_activity(
    activity: ActivityMode,
    time: str = "now",
    duration_minutes: Optional[float] = None,
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a finished activity (bath, tummy time, story time, ...); time is when it started."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(
            api.log_activity(kid["id"], mode=activity, start_time=at, duration=_minutes(duration_minutes), notes=notes)
        )
    return _logged("activity", kid, at, tz, activity=activity, minutes=duration_minutes)


@mcp.tool(annotations=LOG)
async def log_growth(
    weight: Optional[float] = None,
    height: Optional[float] = None,
    head: Optional[float] = None,
    units: Literal["metric", "imperial"] = "metric",
    time: str = "now",
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log growth measurements: metric is kg and cm, imperial is lbs and inches. Give at least one."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(api.log_growth(kid["id"], start_time=at, weight=weight, height=height, head=head, units=units))
    return _logged("growth", kid, at, tz, weight=weight, height=height, head=head, units=units)


@mcp.tool(annotations=LOG)
async def log_temperature(
    amount: float,
    units: TemperatureUnits,
    time: str = "now",
    notes: Optional[str] = None,
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a body temperature reading (units C or F)."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    async with _api(tz) as api:
        kid = await _child(api, child)
        await _call(api.log_temperature(kid["id"], start_time=at, amount=amount, units=units, notes=notes))
    return _logged("temperature", kid, at, tz, temperature=f"{amount:g} °{units}")


@mcp.tool(annotations=LOG)
async def log_medicine(
    medicine: str,
    amount: Optional[float] = None,
    units: Optional[MedicationUnits] = None,
    time: str = "now",
    notes: str = "",
    child: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a medicine dose. medicine is a name or id from list_medicines; amount and units are optional."""
    tz = _tz(timezone_name)
    at = _time(time, tz)
    wanted = medicine.strip().lower()
    async with _api(tz) as api:
        kid = await _child(api, child)
        types = [t for t in await _call(api.list_medicine_types(kid["id"])) if t.active]
        matches = [t for t in types if wanted in (t.id_.lower(), t.name.lower())]
        if len(matches) != 1:
            known = ", ".join(t.name for t in types) or "none"
            raise ToolError(f"No medicine named '{medicine}' (set up: {known}); add it with create_medicine first")
        await _call(
            api.log_medicine(
                kid["id"], start_time=at, medicine_type=matches[0], amount=amount, units=units, notes=notes
            )
        )
    dose = f"{amount:g} {units or ''}".strip() if amount is not None else None
    return _logged("medicine", kid, at, tz, medicine=matches[0].name, dose=dose)


@mcp.tool(annotations=LOG)
async def create_medicine(name: str, child: Optional[str] = None) -> Dict[str, Any]:
    """Add a medicine to a child's list so it can be logged with log_medicine."""
    async with _api(_tz(None)) as api:
        kid = await _child(api, child)
        created = await _call(api.create_medicine_type(kid["id"], name.strip()))
    return {"created": created.name, "id": created.id_, "child": kid["name"]}


@mcp.tool(annotations=LOG)
async def create_custom_food(name: str, child: Optional[str] = None) -> Dict[str, Any]:
    """Add a custom solid food for a child, for foods not in Huckleberry's list (check find_solid_foods first)."""
    async with _api(_tz(None)) as api:
        kid = await _child(api, child)
        created = await _call(api.create_solids_custom_food(kid["id"], name))
    return {"created": created.name, "id": created.id, "source": created.source, "child": kid["name"]}
