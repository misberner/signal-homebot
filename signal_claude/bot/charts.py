"""Charts of Home Assistant history, drawn by the bot (not by Claude).

Claude only names the entities and the time span; the bot reads HA's
long-term statistics (5-minute, hourly or daily aggregates) through ha-mcp,
falls back to the raw state history for entities without statistics (e.g.
on/off sensors), and renders a PNG with matplotlib.
"""
from __future__ import annotations
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .core import ENTITY_RE

MAX_CHARTS = 2
MAX_SERIES = 6
MAX_HOURS = 90 * 24
MAX_AXES = 3
BINARY = {'on': 1.0, 'off': 0.0, 'open': 1.0, 'closed': 0.0, 'home': 1.0, 'not_home': 0.0,
          'true': 1.0, 'false': 0.0, 'detected': 1.0, 'clear': 0.0}


class ChartError(ValueError):
    """Not drawable; the message is safe to show users."""


@dataclass
class Series:
    entity_id: str
    label: str
    unit: str
    kind: str                      # 'band' (mean with min/max), 'bars' (change per period), 'step', 'binary'
    x: list = field(default_factory=list)
    y: list = field(default_factory=list)
    lo: list = field(default_factory=list)
    hi: list = field(default_factory=list)
    width: float = 0.0             # bar width in days (matplotlib date units)


def check_spec(spec: object) -> tuple[str, list[str], int]:
    """A chart Claude asked for: {"title", "entity_ids", "hours"}."""
    if not isinstance(spec, dict) or set(spec) - {'title', 'entity_ids', 'hours'}:
        raise ChartError('a chart has a title, entity_ids and hours')
    title, ids, hours = spec.get('title') or '', spec.get('entity_ids'), spec.get('hours')
    if not isinstance(title, str) or len(title) > 80:
        raise ChartError('chart title: at most 80 characters')
    if (not isinstance(ids, list) or not 0 < len(ids) <= MAX_SERIES
            or not all(isinstance(e, str) and re.fullmatch(ENTITY_RE, e) for e in ids)):
        raise ChartError(f'a chart shows 1-{MAX_SERIES} entities')
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_HOURS:
        raise ChartError(f'a chart covers 1-{MAX_HOURS} hours')
    return title.strip(), list(dict.fromkeys(ids)), hours


def period_for(hours: int) -> str:
    """The statistics period that keeps a chart within ~1000 points."""
    return '5minute' if hours <= 72 else 'hour' if hours <= 45 * 24 else 'day'


def _entities(text: str) -> list[dict]:
    data = json.loads(text)
    data = data.get('data', data) if isinstance(data, dict) else {}
    entities = data.get('entities') if isinstance(data, dict) else None
    return [e for e in entities if isinstance(e, dict)] if isinstance(entities, list) else []


def _local(ms: object) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000).astimezone() if isinstance(ms, (int, float)) else None


def statistics_series(text: str, meta: dict[str, dict]) -> dict[str, Series]:
    """Series from an ha_get_history(source='statistics') result; entities
    without rows are left out."""
    out = {}
    for entity in _entities(text):
        rows = [r for r in entity.get('statistics') or [] if isinstance(r, dict) and _local(r.get('start'))]
        eid = entity.get('entity_id')
        if not rows or not isinstance(eid, str):
            continue
        label, unit = meta.get(eid, {}).get('label', eid), meta.get(eid, {}).get('unit', '')
        if any(isinstance(r.get('mean'), (int, float)) for r in rows):
            s = Series(eid, label, unit, 'band')
            for r in rows:
                if isinstance(r.get('mean'), (int, float)):
                    s.x.append(_local(r['start']))
                    s.y.append(float(r['mean']))
                    s.lo.append(float(r['min']) if isinstance(r.get('min'), (int, float)) else float(r['mean']))
                    s.hi.append(float(r['max']) if isinstance(r.get('max'), (int, float)) else float(r['mean']))
        elif any(isinstance(r.get('change'), (int, float)) for r in rows):
            s = Series(eid, label, unit, 'bars')
            for r in rows:
                if isinstance(r.get('change'), (int, float)):
                    s.x.append(_local(r['start']))
                    s.y.append(float(r['change']))
            spans = [(r['end'] - r['start']) for r in rows if isinstance(r.get('end'), (int, float))]
            s.width = (min(spans) if spans else 3_600_000) / 86_400_000 * 0.8
        else:
            continue
        if s.x:
            out[eid] = s
    return out


def history_series(text: str, meta: dict[str, dict]) -> dict[str, Series]:
    """Series from raw state history (numbers, or on/off-like states)."""
    out = {}
    for entity in _entities(text):
        eid = entity.get('entity_id')
        points = []
        for st in entity.get('states') or []:
            when = st.get('last_changed') or st.get('last_updated') if isinstance(st, dict) else None
            try:
                points.append((datetime.fromisoformat(when).astimezone(), str(st.get('state'))))
            except (TypeError, ValueError):
                continue
        if not isinstance(eid, str) or not points:
            continue
        points.sort()
        label, unit = meta.get(eid, {}).get('label', eid), meta.get(eid, {}).get('unit', '')
        values = [v for _, v in points if v not in ('unavailable', 'unknown')]
        if values and all(v.lower() in BINARY for v in values):
            s = Series(eid, label, '', 'binary')
            for when, v in points:
                if v.lower() in BINARY:
                    s.x.append(when)
                    s.y.append(BINARY[v.lower()])
        else:
            s = Series(eid, label, unit, 'step')
            for when, v in points:
                try:
                    s.y.append(float(v))
                    s.x.append(when)
                except ValueError:
                    continue
        if s.x:
            out[eid] = s
    return out


def render(title: str, series: list[Series], start: datetime, end: datetime) -> bytes:
    """A PNG with one panel per unit (on/off sensors share a small panel)."""
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    from matplotlib.figure import Figure
    if not series:
        raise ChartError('no data for that time')
    groups: dict[str, list[Series]] = {}
    for s in series:
        groups.setdefault('\0binary' if s.kind == 'binary' else s.unit, []).append(s)
    keys = sorted(groups, key=lambda k: k == '\0binary')[:MAX_AXES]
    ratios = [0.45 if k == '\0binary' else 1.0 for k in keys]
    fig = Figure(figsize=(10, 1.2 + 2.6 * sum(ratios)), dpi=110, layout='constrained')
    axes = fig.subplots(len(keys), 1, sharex=True, squeeze=False, gridspec_kw={'height_ratios': ratios})[:, 0]
    colors = iter(f'C{i}' for i in range(10))
    for ax, key in zip(axes, keys):
        for s in groups[key]:
            color = next(colors)
            if s.kind == 'band':
                ax.plot(s.x, s.y, color=color, linewidth=1.4, label=s.label)
                ax.fill_between(s.x, s.lo, s.hi, color=color, alpha=0.15, linewidth=0)
            elif s.kind == 'bars':
                ax.bar(s.x, s.y, width=s.width, align='edge', color=color, alpha=0.8, label=s.label)
            else:
                xs, ys = s.x + [end], s.y + [s.y[-1]]  # hold the last state until now
                ax.step(xs, ys, where='post', color=color, linewidth=1.4, label=s.label)
        if key == '\0binary':
            ax.set_yticks([0, 1], ['off', 'on'])
            ax.set_ylim(-0.15, 1.15)
        elif key:
            ax.set_ylabel(key)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='upper left', fontsize=8, frameon=False)
    locator = AutoDateLocator(tz=start.tzinfo)
    axes[-1].xaxis.set_major_locator(locator)
    # Numeric dates (23.10.), not English month names.
    axes[-1].xaxis.set_major_formatter(ConciseDateFormatter(
        locator, tz=start.tzinfo, formats=['%Y', '%m.%Y', '%d.%m.', '%H:%M', '%H:%M', '%S'],
        zero_formats=['', '%m.%Y', '%d.%m.', '%d.%m.', '%H:%M', '%H:%M'],
        offset_formats=['', '%Y', '%m.%Y', '%d.%m.%Y', '%d.%m.%Y', '%d.%m.%Y %H:%M']))
    axes[-1].set_xlim(start, end)
    if title:
        fig.suptitle(title, fontsize=12)
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    return buf.getvalue()
