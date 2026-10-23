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
    """A line chart Claude asked for: {"title", "entity_ids", "hours"} (and "kind": "line")."""
    if not isinstance(spec, dict) or set(spec) - {'title', 'entity_ids', 'hours', 'kind'}:
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


def check_flow_spec(spec: object) -> tuple[str, str]:
    """An energy flow chart: {"title", "kind": "energy_flow", "span"}."""
    if not isinstance(spec, dict) or set(spec) - {'title', 'kind', 'span', 'entity_ids', 'hours'}:
        raise ChartError('an energy flow chart has a title and a span')
    title, span = spec.get('title') or '', spec.get('span') or 'today'
    if not isinstance(title, str) or len(title) > 80:
        raise ChartError('chart title: at most 80 characters')
    if span not in FLOW_SPANS:
        raise ChartError(f'span must be one of {", ".join(FLOW_SPANS)}')
    return title.strip(), span


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


# ---------------------------------------------------------------- energy flow

FLOW_SPANS = ('now', 'today', 'yesterday', 'week', 'month')


def find_energy_sources(text: str) -> list[dict]:
    """The energy_sources list from the energy dashboard prefs, wherever the
    tool result nests it."""
    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('energy_sources'), list):
                return node['energy_sources']
            for value in node.values():
                if (found := walk(value)) is not None:
                    return found
        return None
    found = walk(json.loads(text))
    return [s for s in found if isinstance(s, dict)] if found else []


def energy_sensors(sources: list[dict]) -> dict[str, list[str]]:
    """Statistic ids per role from the energy prefs: solar, grid_in, grid_out,
    battery_in (charging), battery_out (discharging), and the power sensors
    (solar_rate, grid_rate, battery_rate) where HA has them configured."""
    roles = {k: [] for k in ('solar', 'grid_in', 'grid_out', 'battery_in', 'battery_out',
                             'solar_rate', 'grid_rate', 'battery_rate')}

    def add(role, value):
        if isinstance(value, str) and re.fullmatch(ENTITY_RE + r'|[a-z_]+:[a-z0-9_]+', value) \
                and value not in roles[role]:
            roles[role].append(value)
    for source in sources:
        kind = source.get('type')
        if kind == 'solar':
            add('solar', source.get('stat_energy_from'))
            add('solar_rate', source.get('stat_rate'))
        elif kind == 'battery':
            add('battery_out', source.get('stat_energy_from'))
            add('battery_in', source.get('stat_energy_to'))
            add('battery_rate', source.get('stat_rate'))
        elif kind == 'grid':
            # Older prefs list flows; newer ones put the sensors on the source itself.
            add('grid_in', source.get('stat_energy_from'))
            add('grid_out', source.get('stat_energy_to'))
            add('grid_rate', source.get('stat_rate'))
            for flow in source.get('flow_from') or []:
                if isinstance(flow, dict):
                    add('grid_in', flow.get('stat_energy_from'))
            for flow in source.get('flow_to') or []:
                if isinstance(flow, dict):
                    add('grid_out', flow.get('stat_energy_to'))
    return roles


def split_flows(solar: float, grid_in: float, grid_out: float, battery_in: float,
                battery_out: float) -> dict[str, float]:
    """Who supplied whom, as HA's energy distribution card assumes: exports
    come from solar first, the battery charges from solar first."""
    solar, grid_in, grid_out, battery_in, battery_out = (max(0.0, v) for v in
                                                         (solar, grid_in, grid_out, battery_in, battery_out))
    solar_to_grid = min(grid_out, solar)
    battery_to_grid = min(grid_out - solar_to_grid, battery_out)
    solar_to_battery = min(battery_in, solar - solar_to_grid)
    grid_to_battery = min(battery_in - solar_to_battery, grid_in)
    return {
        'solar_home': max(0.0, solar - solar_to_grid - solar_to_battery),
        'solar_grid': solar_to_grid, 'solar_battery': solar_to_battery,
        'grid_home': max(0.0, grid_in - grid_to_battery), 'grid_battery': grid_to_battery,
        'battery_home': max(0.0, battery_out - battery_to_grid), 'battery_grid': battery_to_grid,
    }


def render_flow(title: str, totals: dict[str, float], unit: str, has: set[str]) -> bytes:
    """Home in the middle, solar above, grid left, battery below; arrows from
    box edge to box edge, sized by what flowed (no crossings)."""
    from matplotlib.figure import Figure
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    flows = split_flows(totals.get('solar', 0), totals.get('grid_in', 0), totals.get('grid_out', 0),
                        totals.get('battery_in', 0), totals.get('battery_out', 0))
    home = flows['solar_home'] + flows['grid_home'] + flows['battery_home']
    w, h = 0.11, 0.065  # half box size
    pos = {'solar': (0.5, 0.86), 'grid': (0.15, 0.5), 'home': (0.5, 0.5), 'battery': (0.5, 0.14)}
    names = {'solar': 'Solar', 'grid': 'Grid', 'home': 'Home', 'battery': 'Battery'}
    colors = {'solar': '#e3a008', 'grid': '#5b6b7f', 'home': '#1f6feb', 'battery': '#1a7f37'}

    def side(node, where):
        x, y = pos[node]
        return {'top': (x, y + h), 'bottom': (x, y - h), 'left': (x - w, y), 'right': (x + w, y)}[where]
    # (from, to, rad): solar→battery bends around the right of home.
    routes = {'solar_home': (side('solar', 'bottom'), side('home', 'top'), 0),
              'grid_home': (side('grid', 'right'), side('home', 'left'), 0),
              'battery_home': (side('battery', 'top'), side('home', 'bottom'), 0),
              'solar_grid': (side('solar', 'left'), side('grid', 'top'), 0.3),
              'grid_battery': (side('grid', 'bottom'), side('battery', 'left'), 0.3),
              'battery_grid': (side('battery', 'left'), side('grid', 'bottom'), -0.3),
              'solar_battery': (side('solar', 'right'), side('battery', 'right'), -0.55)}
    big = max([home, *totals.values(), 0])
    fmt = (lambda v: f'{v:.2f} {unit}') if big < 10 else (lambda v: f'{v:.1f} {unit}')
    fig = Figure(figsize=(6.6, 6.6), dpi=110, layout='constrained')
    ax = fig.add_subplot()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect('equal')
    ax.axis('off')
    biggest = max([*flows.values(), 1e-9])
    for key, ((x1, y1), (x2, y2), rad) in routes.items():
        value = flows[key]
        if value <= biggest * 0.005:
            continue
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), connectionstyle=f'arc3,rad={rad}', arrowstyle='-|>',
                                     mutation_scale=12 + 6 * value / biggest, shrinkA=2, shrinkB=2,
                                     linewidth=1 + 6 * value / biggest, color=colors[key.split('_')[0]], alpha=0.8))
        # Label on the curve's midpoint (matplotlib's arc3 control point, halfway).
        dx, dy = x2 - x1, y2 - y1
        lx, ly = (x1 + x2) / 2 + rad * dy / 2, (y1 + y2) / 2 - rad * dx / 2
        if not rad and not dy:  # the short horizontal arrow: label above it
            ly += 0.035
        ax.text(lx, ly, fmt(value), ha='center', va='center', fontsize=9,
                bbox={'boxstyle': 'round,pad=0.2', 'fc': 'white', 'ec': 'none', 'alpha': 0.9})
    detail = {'solar': fmt(totals.get('solar', 0)),
              'grid': f'in {fmt(totals.get("grid_in", 0))}\nout {fmt(totals.get("grid_out", 0))}',
              'battery': f'in {fmt(totals.get("battery_in", 0))}\nout {fmt(totals.get("battery_out", 0))}',
              'home': fmt(home)}
    for key, (x, y) in pos.items():
        if key != 'home' and key not in has:
            continue
        ax.add_patch(FancyBboxPatch((x - w, y - h), 2 * w, 2 * h, boxstyle='round,pad=0,rounding_size=0.025',
                                    fc='white', ec=colors[key], linewidth=2.2))
        ax.text(x, y + 0.028, names[key], ha='center', va='center', fontsize=11, weight='bold', color=colors[key])
        ax.text(x, y - 0.022, detail[key], ha='center', va='center', fontsize=8)
    if title:
        fig.suptitle(title, fontsize=12)
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    return buf.getvalue()
