"""Named schedule sets, e.g. "Performance mode": hold some RA2 schedules off, then put them back.

A set lists schedules to disable (and optionally enable) while it is active. Activating
records each affected schedule's prior enabled state in the config *before* anything is
sent, so a crash or restart mid-show never loses it; restoring puts back exactly that
prior state (schedules that were already in the wanted state are left alone and stay
that way). Schedules are remembered by id and by name, so ones that were renamed or that
vanished from the Lutron system are handled instead of silently hitting the wrong thing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from .config import ActiveScheduleEntry, ActiveScheduleSet, ScheduleRef, ScheduleSetConfig
from .models import Schedule

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)


class ScheduleSetError(Exception):
    """A request the user can fix; ``status`` is the HTTP status the web layer should use."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def resolve_schedule(ref: ScheduleRef, schedules: list[Schedule]) -> tuple[Schedule | None, str | None]:
    """Find the live schedule a saved reference points at, or say why not.

    The id wins when the name still matches. If the id now carries a different name, it is
    treated as a rename, unless another schedule has the saved name (then it could equally
    be a renumbering, so it is skipped rather than guessed). With no id match, a single
    schedule with the saved name is accepted.
    """
    label = ref.name or ref.id
    by_id = next((s for s in schedules if s.id == ref.id), None)
    if by_id is not None:
        if not ref.name or by_id.name == ref.name:
            return by_id, None
        elsewhere = [s for s in schedules if s.name == ref.name and s.id != by_id.id]
        if elsewhere:
            return None, (f"\"{label}\" is ambiguous: id {ref.id} is now \"{by_id.name}\" and "
                          f"\"{ref.name}\" exists as id {elsewhere[0].id} - skipped, edit the set to fix")
        return by_id, None
    by_name = [s for s in schedules if ref.name and s.name == ref.name]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, f"\"{label}\" is no longer id {ref.id} and several schedules share that name - skipped"
    return None, f"\"{label}\" (id {ref.id}) was not found in the Lutron system - skipped"


class ScheduleSetManager:
    def __init__(self, controller: "Controller") -> None:
        self.c = controller
        self._lock = asyncio.Lock()

    # -- helpers ---------------------------------------------------------
    @property
    def sets(self) -> list[ScheduleSetConfig]:
        return self.c.config.schedule_sets

    @property
    def active(self) -> ActiveScheduleSet | None:
        return self.c.config.active_schedule_set

    def get(self, set_id: str) -> ScheduleSetConfig:
        s = next((x for x in self.sets if x.id == set_id), None)
        if s is None:
            raise KeyError(set_id)
        return s

    def _driver(self):
        drv = self.c.driver
        if not drv.connected:
            raise ScheduleSetError(502, "Not connected to the Lutron system - nothing was changed")
        return drv

    def status(self) -> dict | None:
        a = self.active
        if a is None:
            return None
        return {"set_id": a.set_id, "set_name": a.set_name, "activated_at": a.activated_at,
                "count": len(a.entries), "schedules": [e.name for e in a.entries],
                "warnings": list(a.warnings)}

    # -- editing ---------------------------------------------------------
    def _refs(self, ids: list[str], old: list[ScheduleRef]) -> list[ScheduleRef]:
        live = {s.id: s for s in self.c.driver.schedules()}
        kept = {r.id: r for r in old}
        out: list[ScheduleRef] = []
        for sid in dict.fromkeys(ids):
            if sid in live:
                out.append(ScheduleRef(id=sid, name=live[sid].name))
            elif sid in kept:  # vanished from the system but already part of this set: keep it
                out.append(kept[sid])
            else:
                raise ScheduleSetError(404, f"Unknown schedule {sid!r}")
        return out

    def save(self, name: str, disable: list[str], enable: list[str], set_id: str | None = None) -> ScheduleSetConfig:
        name = name.strip()
        if not name:
            raise ScheduleSetError(422, "A schedule set needs a name")
        if any(s.name.casefold() == name.casefold() and s.id != set_id for s in self.sets):
            raise ScheduleSetError(409, f"A schedule set called \"{name}\" already exists")
        both = set(disable) & set(enable)
        if both:
            raise ScheduleSetError(422, "A schedule cannot be both disabled and enabled by the same set")
        existing = self.get(set_id) if set_id else None
        dis = self._refs(disable, existing.disable if existing else [])
        ena = self._refs(enable, existing.enable if existing else [])
        if not dis and not ena:
            raise ScheduleSetError(422, "Pick at least one schedule for the set")
        if existing:
            existing.name, existing.disable, existing.enable = name, dis, ena
            sset = existing
            if self.active and self.active.set_id == sset.id:
                self.active.set_name = name
        else:
            sset = ScheduleSetConfig(id=uuid.uuid4().hex[:8], name=name, disable=dis, enable=ena)
            self.sets.append(sset)
        self.c.save()
        return sset

    def delete(self, set_id: str) -> None:
        sset = self.get(set_id)
        if self.active and self.active.set_id == set_id:
            raise ScheduleSetError(409, f"\"{sset.name}\" is active - restore the schedules before deleting it")
        self.c.config.schedule_sets = [s for s in self.sets if s.id != set_id]
        self.c.save()

    # -- activate / restore ----------------------------------------------
    async def _apply(self, drv, wanted: dict[str, bool]) -> tuple[dict[str, str], set[str]]:
        """Send each change, then re-read the system. Returns ({id: error}, ids with no confirmation)."""
        errors: dict[str, str] = {}
        for sid, enabled in wanted.items():
            try:
                await drv.set_schedule_enabled(sid, enabled)
            except Exception as exc:  # one bad schedule must not stop the rest
                log.warning("Could not set schedule %s to %s: %s", sid, enabled, exc)
                errors[sid] = str(exc) or exc.__class__.__name__
        sent = [sid for sid in wanted if sid not in errors]
        fresh: set[str] = set()
        if sent:
            try:
                fresh = await drv.refresh_schedules(sent)
            except Exception as exc:
                log.warning("Could not re-read schedules: %s", exc)
        by_id = {s.id: s for s in drv.schedules()}
        for sid in sent:
            if sid in fresh and by_id[sid].enabled != wanted[sid]:
                errors[sid] = f"the system still reports it as {'enabled' if by_id[sid].enabled else 'disabled'}"
        unconfirmed = {sid for sid in sent if sid not in fresh}
        return errors, unconfirmed

    async def activate(self, set_id: str) -> dict:
        async with self._lock:
            cfg = self.c.config
            if cfg.active_schedule_set is not None:
                raise ScheduleSetError(
                    409, f"\"{cfg.active_schedule_set.set_name}\" is already active - restore the schedules first")
            sset = self.get(set_id)
            drv = self._driver()
            schedules = list(drv.schedules())

            wanted: dict[str, bool] = {}
            missing: list[str] = []
            for refs, want in ((sset.disable, False), (sset.enable, True)):
                for ref in refs:
                    sched, problem = resolve_schedule(ref, schedules)
                    if sched is None:
                        missing.append(problem or ref.name)
                    elif sched.id not in wanted:
                        wanted[sched.id] = want
            if not wanted:
                raise ScheduleSetError(
                    409, "None of this set's schedules could be found in the Lutron system: " + "; ".join(missing))

            # Prior state must come from the system, not a stale cache.
            await drv.refresh_schedules(list(wanted))
            by_id = {s.id: s for s in drv.schedules()}
            entries = [ActiveScheduleEntry(id=sid, name=by_id[sid].name, previous=by_id[sid].enabled, desired=want)
                       for sid, want in wanted.items() if by_id[sid].enabled != want]
            unchanged = [by_id[sid].name for sid, want in wanted.items() if by_id[sid].enabled == want]

            # Write-ahead: the restore info is on disk before the first command goes out.
            active = ActiveScheduleSet(set_id=sset.id, set_name=sset.name, entries=entries,
                                       activated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            cfg.active_schedule_set = active
            self.c.save()

            errors, unconfirmed = await self._apply(drv, {e.id: e.desired for e in entries})
            if entries and len(errors) == len(entries):
                cfg.active_schedule_set = None  # nothing took effect, so there is nothing to restore
                self.c.save()
                raise ScheduleSetError(502, "No schedules could be changed, so the set was not activated: " + "; ".join(
                    f"{by_id[i].name}: {m}" for i, m in errors.items()))

            failures = [f"{by_id[i].name}: {m}" for i, m in errors.items()]
            names = {e.id: e.name for e in entries}
            result = {
                "ok": not failures and not missing,
                "active": True,
                "changed": [e.name for e in entries if e.id not in errors],
                "unchanged": unchanged,
                "missing": missing,
                "failures": failures,
                "unconfirmed": sorted(names[i] for i in unconfirmed),
            }
            active.warnings = ([f"Could not change {f}" for f in failures] + [f"Skipped: {m}" for m in missing]
                               + [f"No confirmation from the system for {n}" for n in result["unconfirmed"]])
            self.c.save()
            return result

    async def deactivate(self) -> dict:
        async with self._lock:
            cfg = self.c.config
            active = cfg.active_schedule_set
            if active is None:
                raise ScheduleSetError(404, "No schedule set is active")
            drv = self._driver()
            schedules = list(drv.schedules())

            wanted: dict[str, bool] = {}
            missing: list[str] = []
            for e in active.entries:
                sched, problem = resolve_schedule(ScheduleRef(e.id, e.name), schedules)
                if sched is None:
                    missing.append(problem or e.name)
                else:
                    wanted[sched.id] = e.previous

            errors, unconfirmed = await self._apply(drv, wanted)
            by_id = {s.id: s for s in drv.schedules()}
            if errors:
                # keep exactly the ones still unrestored so the button can be pressed again
                active.entries = [e for e in active.entries if _resolved_id(e, schedules) in errors]
                failures = [f"{by_id[i].name}: {m}" for i, m in errors.items()]
                active.warnings = [f"Could not restore {f}" for f in failures]
                self.c.save()
            else:
                failures = []
                cfg.active_schedule_set = None
                self.c.save()
            return {
                "ok": not failures and not missing,
                "active": cfg.active_schedule_set is not None,
                "restored": [by_id[i].name for i in wanted if i not in errors],
                "missing": missing,
                "failures": failures,
                "unconfirmed": sorted(by_id[i].name for i in unconfirmed),
            }

    def forget_active(self) -> None:
        """Drop the saved prior states without restoring (for when the schedules were fixed by hand)."""
        if self.active is None:
            raise ScheduleSetError(404, "No schedule set is active")
        self.c.config.active_schedule_set = None
        self.c.save()


def _resolved_id(entry: ActiveScheduleEntry, schedules: list[Schedule]) -> str | None:
    sched, _ = resolve_schedule(ScheduleRef(entry.id, entry.name), schedules)
    return sched.id if sched else None
