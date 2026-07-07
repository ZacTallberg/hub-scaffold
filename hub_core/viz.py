"""viz.py — dependency-free inline-SVG dataviz helpers for the project hubs.

Stack-neutral, stdlib only. Each function returns a complete ``<svg>`` STRING that can be
dropped straight into a Django template (``{{ svg|safe }}``) OR a single-file WSGI f-string page.
No matplotlib, no chart lib, no numpy, no deps. Identical bytes across every project hub.

The four helpers (hub doctrine §8 / §11 "frontend kit"):
  - ``sparkline(values, ...)``  — fps / drift / store-size trend off the /debug ring.
  - ``progress(done, total, ...)`` — segmented phase / coverage bar.
  - ``donut(pct, label, ...)``  — the headline audit % ring.
  - ``heatmap(grid, ...)``      — deploy calendar / FEATURES status matrix rect-grid.

Theming contract (consumes the SAME tokens.css custom properties as the rest of the kit, so
one ``--accent-h`` hue knob + ``light-dark()`` repaints the charts automatically):
  --surface-2, --border, --text, --muted, --accent, and the 5 status roles
  --pass / --warn / --fail / --info / --stale (each a *-bg / *-fg pair).
No hard-coded hex anywhere — colours flow from CSS vars, with a literal fallback inside each
``var(--token, <fallback>)`` so the SVG still renders if dropped on a page without tokens.css.

Accessibility (WCAG 2.2 AA, doctrine §10): every chart is ``role="img"`` with a REQUIRED,
caller-supplied ``label`` surfaced as BOTH ``aria-label`` and an inner ``<title>`` (the
title id is wired via ``aria-labelledby`` for the most reliable SR support); decorative inner
shapes are ``aria-hidden``; status is encoded by GLYPH + label, never colour alone
(pass=u2713 warn=u25B2 fail=u2715 info=u2022 stale=u25CC). ``focusable="false"`` +
``tabindex="-1"`` keep charts out of the tab order. Stroked trends use
``vector-effect="non-scaling-stroke"`` so the line stays crisp at any rendered size.

Every dynamic value that lands in markup is escaped (``escape_attr`` / ``escape_text``) and
every coordinate is emitted through ``_num`` (finite-checked, NaN/inf-safe, short decimals).
"""
from __future__ import annotations

import itertools
from html import escape
from typing import Iterable, Mapping, Optional, Sequence, Union

__all__ = [
    "STATUS_GLYPHS",
    "sparkline",
    "progress",
    "donut",
    "heatmap",
    "escape_attr",
    "escape_text",
]

# ---------------------------------------------------------------------------
# Status vocabulary — FIXED 5-state, glyph + role token. Mirrors tokens.css and
# the JS kit. NEVER rendered as colour alone. Keep these byte-identical to the
# doctrine SHARED CONTRACT.
# ---------------------------------------------------------------------------
STATUS_GLYPHS: dict[str, str] = {
    "pass": "✓",   # ✓
    "warn": "▲",   # ▲
    "fail": "✕",   # ✕
    "info": "•",   # •
    "stale": "◌",  # ◌
}

# Tokens consumed for status fills, with literal fallbacks (used only when a page
# lacks tokens.css). Fallbacks are mid-tone, AA-against-surface, hue-stable hexes.
_STATUS_TOKEN: dict[str, tuple[str, str]] = {
    # role: (css-var name, literal fallback)
    "pass": ("--pass", "#3a9b5c"),
    "warn": ("--warn", "#b8860b"),
    "fail": ("--fail", "#c0392b"),
    "info": ("--info", "#2b7bb9"),
    "stale": ("--stale", "#8a8f98"),
}

# Neutral surface / structural tokens, with literal fallbacks.
_BORDER = "var(--border, #d0d3d9)"
_SURFACE2 = "var(--surface-2, #eceef2)"
_TEXT = "var(--text, #1a1d23)"
_MUTED = "var(--muted, #6b7280)"
_ACCENT = "var(--accent, #4060c0)"

# A unique counter so multiple charts on one page get unique <title>/clip ids
# (aria-labelledby must reference a DOM-unique id).
_ID_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# Escaping + numeric helpers
# ---------------------------------------------------------------------------
def escape_attr(value: object) -> str:
    """Escape an arbitrary value for use inside a double-quoted SVG/HTML attribute."""
    return escape(str(value), quote=True)


def escape_text(value: object) -> str:
    """Escape an arbitrary value for use as SVG/HTML text content (no quote escaping)."""
    return escape(str(value), quote=False)


def _num(x: object, default: float = 0.0) -> str:
    """Format a coordinate/length: finite floats only, trimmed to <=3 decimals.

    NaN / +-inf / non-numeric collapse to ``default`` so a poisoned input can never
    emit ``NaN`` / ``Infinity`` into the SVG (which would silently break rendering).
    """
    try:
        f = float(x)
    except (TypeError, ValueError):
        f = float(default)
    if f != f or f in (float("inf"), float("-inf")):  # NaN or inf
        f = float(default)
    if f == int(f):
        return str(int(f))
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _status_fill(status: Optional[str]) -> str:
    """Resolve a status role to a ``var(--role, fallback)`` fill string.

    Unknown / falsy status falls back to the neutral surface-2 token so an
    unexpected value renders inert rather than mis-cued.
    """
    key = str(status).strip().lower() if status is not None else ""
    if key in _STATUS_TOKEN:
        var, fallback = _STATUS_TOKEN[key]
        return f"var({var}, {fallback})"
    return _SURFACE2


def _coerce_floats(values: Iterable[object]) -> list[float]:
    """Best-effort coerce an iterable to finite floats; non-finite/non-numeric -> 0.0."""
    out: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        if f != f or f in (float("inf"), float("-inf")):
            f = 0.0
        out.append(f)
    return out


def _svg_open(
    w: float,
    h: float,
    label: str,
    *,
    extra_class: str = "",
    title_id: Optional[str] = None,
) -> tuple[str, str]:
    """Build the shared opening ``<svg ...>`` + inner ``<title>`` and return (open, title_id).

    Wires role=img + aria-labelledby->title (most reliable SR pattern) AND a redundant
    aria-label; keeps the graphic out of the tab order; preserves aspect on scale.
    """
    if title_id is None:
        title_id = f"vz{next(_ID_SEQ)}"
    safe_label = escape_attr(label)
    cls = f' class="{escape_attr(extra_class)}"' if extra_class else ""
    open_tag = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_num(w)} {_num(h)}" '
        f'width="{_num(w)}" height="{_num(h)}"{cls} '
        f'role="img" aria-labelledby="{title_id}" aria-label="{safe_label}" '
        f'preserveAspectRatio="xMidYMid meet" focusable="false" tabindex="-1">'
        f'<title id="{title_id}">{escape_text(label)}</title>'
    )
    return open_tag, title_id


# ===========================================================================
# 1. SPARKLINE — fps / drift / store-size trend (off the /debug ring)
# ===========================================================================
def sparkline(
    values: Sequence[Union[int, float]],
    w: float = 120,
    h: float = 32,
    *,
    label: str = "trend",
    stroke: str = _ACCENT,
    fill: bool = True,
    show_last: bool = True,
    pad: float = 2.0,
    stroke_width: float = 1.5,
    baseline: Optional[float] = None,
) -> str:
    """Return an inline-SVG sparkline for a 1-D series.

    Uses ``vector-effect="non-scaling-stroke"`` so the polyline stays a crisp
    ``stroke_width`` px regardless of the rendered/scaled size (doctrine §8/§11).

    Args:
      values: the series (any finite numbers; non-finite coerced to 0).
      w, h: intrinsic SVG size in px (also the viewBox; scales fluidly via CSS).
      label: REQUIRED accessible name -> aria-label + <title> (e.g. "FPS, last 60s").
      stroke: line colour (a CSS colour or ``var(--token, fallback)``; default --accent).
      fill: if True, fills under the line with a faint translucent wash of ``stroke``.
      show_last: if True, draws a dot at the most-recent point.
      pad: inner padding in px so the stroke/dot don't clip at the edges.
      stroke_width: nominal (non-scaling) stroke width in px.
      baseline: optional reference value; drawn as a dashed muted guide line.

    Degenerate inputs (empty / single point / flat series) render a centred flat
    guide line rather than a broken path, so a fresh ring never errors the page.
    """
    open_tag, _tid = _svg_open(w, h, label, extra_class="hub-sparkline")
    data = _coerce_floats(values)

    inner_w = max(1.0, float(w) - 2 * pad)
    inner_h = max(1.0, float(h) - 2 * pad)
    mid_y = float(h) / 2.0

    body: list[str] = []

    # Degenerate: 0 or 1 point -> flat midline.
    if len(data) < 2:
        body.append(
            f'<line x1="{_num(pad)}" y1="{_num(mid_y)}" '
            f'x2="{_num(float(w) - pad)}" y2="{_num(mid_y)}" '
            f'stroke="{escape_attr(stroke)}" stroke-width="{_num(stroke_width)}" '
            f'vector-effect="non-scaling-stroke" stroke-linecap="round" '
            f'opacity="0.5" aria-hidden="true"/>'
        )
        if data and show_last:
            body.append(
                f'<circle cx="{_num(float(w) - pad)}" cy="{_num(mid_y)}" r="{_num(stroke_width + 1)}" '
                f'fill="{escape_attr(stroke)}" aria-hidden="true"/>'
            )
        return open_tag + "".join(body) + "</svg>"

    lo = min(data)
    hi = max(data)
    span = hi - lo
    n = len(data)
    step = inner_w / (n - 1)

    def _x(i: int) -> float:
        return pad + i * step

    def _y(v: float) -> float:
        if span == 0:
            return mid_y  # flat series -> centre line
        # invert: higher value -> higher on screen (smaller y)
        return pad + inner_h - ((v - lo) / span) * inner_h

    pts = [(_x(i), _y(v)) for i, v in enumerate(data)]
    line_pts = " ".join(f"{_num(x)},{_num(y)}" for x, y in pts)

    # Optional area fill under the curve.
    if fill:
        area = (
            f"{_num(pts[0][0])},{_num(float(h) - pad)} "
            + line_pts
            + f" {_num(pts[-1][0])},{_num(float(h) - pad)}"
        )
        body.append(
            f'<polygon points="{area}" fill="{escape_attr(stroke)}" '
            f'fill-opacity="0.12" stroke="none" aria-hidden="true"/>'
        )

    # Optional baseline guide.
    if baseline is not None:
        try:
            by = _y(float(baseline))
            body.append(
                f'<line x1="{_num(pad)}" y1="{_num(by)}" '
                f'x2="{_num(float(w) - pad)}" y2="{_num(by)}" '
                f'stroke="{_MUTED}" stroke-width="1" stroke-dasharray="2 2" '
                f'vector-effect="non-scaling-stroke" opacity="0.6" aria-hidden="true"/>'
            )
        except (TypeError, ValueError):
            pass

    body.append(
        f'<polyline points="{line_pts}" fill="none" '
        f'stroke="{escape_attr(stroke)}" stroke-width="{_num(stroke_width)}" '
        f'stroke-linecap="round" stroke-linejoin="round" '
        f'vector-effect="non-scaling-stroke" aria-hidden="true"/>'
    )

    if show_last:
        lx, ly = pts[-1]
        body.append(
            f'<circle cx="{_num(lx)}" cy="{_num(ly)}" r="{_num(stroke_width + 1)}" '
            f'fill="{escape_attr(stroke)}" aria-hidden="true"/>'
        )

    return open_tag + "".join(body) + "</svg>"


# ===========================================================================
# 2. PROGRESS — segmented bar (phase / coverage)
# ===========================================================================
def progress(
    done: Union[int, float],
    total: Union[int, float],
    w: float = 200,
    h: float = 14,
    *,
    label: Optional[str] = None,
    segments: Optional[Sequence[Mapping[str, object]]] = None,
    status: str = "pass",
    show_text: bool = True,
    gap: float = 1.5,
    radius: Optional[float] = None,
) -> str:
    """Return a segmented progress / coverage bar.

    Two modes:
      * **Scalar** (default): ``done`` of ``total`` filled with the ``status`` role colour
        over a surface-2 track; ``round(pct)%`` printed at the end when ``show_text``.
      * **Segmented**: pass ``segments=[{"value": n, "status": "pass"|...,
        "label": "..."}, ...]`` to render proportional, gap-separated cells in distinct
        status colours (e.g. a phase bar of done/in_progress/blocked) — value is summed for
        the implicit total and each cell carries its own ``<title>`` tooltip.

    The bar is a single ``role="img"`` with the overall accessible name; cell breakdown is
    additionally exposed via per-cell ``<title>``. ``status`` MUST be one of the 5-state
    vocabulary; unknown roles fall back to the neutral surface tone.
    """
    w = float(w)
    h = float(h)
    if radius is None:
        radius = min(h / 2.0, 6.0)

    # ---- Segmented mode ----------------------------------------------------
    if segments:
        seg_list = list(segments)
        vals = [max(0.0, float(s.get("value", 0) or 0)) for s in seg_list]
        seg_total = sum(vals)
        if label is None:
            parts = []
            for s, v in zip(seg_list, vals):
                nm = s.get("label") or s.get("status") or "segment"
                parts.append(f"{escape_text(nm)} {_num(v)}")
            label = "progress: " + ", ".join(parts) if parts else "progress"
        open_tag, _tid = _svg_open(w, h, label, extra_class="hub-progress")

        body = [
            f'<rect x="0" y="0" width="{_num(w)}" height="{_num(h)}" '
            f'rx="{_num(radius)}" ry="{_num(radius)}" fill="{_SURFACE2}" '
            f'stroke="{_BORDER}" stroke-width="1" aria-hidden="true"/>'
        ]
        if seg_total > 0:
            n = len(seg_list)
            total_gap = gap * max(0, n - 1)
            usable = max(0.0, w - total_gap)
            x = 0.0
            for i, (s, v) in enumerate(zip(seg_list, vals)):
                seg_w = (v / seg_total) * usable
                if seg_w <= 0:
                    continue
                fill = _status_fill(s.get("status"))
                seg_label = s.get("label") or s.get("status") or ""
                glyph = STATUS_GLYPHS.get(str(s.get("status", "")).lower(), "")
                title = f"{glyph + ' ' if glyph else ''}{escape_text(seg_label)} ({_num(v)})".strip()
                # rounded only on the true ends of the whole bar
                first = i == 0
                last = i == n - 1
                rx = radius if (first or last) else 0
                body.append(
                    f'<rect x="{_num(x)}" y="0" width="{_num(seg_w)}" height="{_num(h)}" '
                    f'rx="{_num(rx)}" ry="{_num(rx)}" fill="{fill}" aria-hidden="true">'
                    f'<title>{title}</title></rect>'
                )
                x += seg_w + gap
        return open_tag + "".join(body) + "</svg>"

    # ---- Scalar mode -------------------------------------------------------
    try:
        d = float(done)
    except (TypeError, ValueError):
        d = 0.0
    try:
        t = float(total)
    except (TypeError, ValueError):
        t = 0.0
    if d != d:
        d = 0.0
    if t != t:
        t = 0.0
    pct = 0.0 if t <= 0 else max(0.0, min(1.0, d / t))
    pct_int = int(round(pct * 100))

    if label is None:
        label = f"progress {pct_int}% ({_num(d)} of {_num(t)})"
    open_tag, _tid = _svg_open(w, h, label, extra_class="hub-progress")

    fill_w = pct * w
    fill = _status_fill(status)
    body = [
        f'<rect x="0" y="0" width="{_num(w)}" height="{_num(h)}" '
        f'rx="{_num(radius)}" ry="{_num(radius)}" fill="{_SURFACE2}" '
        f'stroke="{_BORDER}" stroke-width="1" aria-hidden="true"/>'
    ]
    if fill_w > 0:
        body.append(
            f'<rect x="0" y="0" width="{_num(fill_w)}" height="{_num(h)}" '
            f'rx="{_num(radius)}" ry="{_num(radius)}" fill="{fill}" aria-hidden="true"/>'
        )
    if show_text:
        # Centred percent text in a contrast-safe colour; lives over the bar.
        body.append(
            f'<text x="{_num(w / 2)}" y="{_num(h / 2)}" '
            f'text-anchor="middle" dominant-baseline="central" '
            f'font-family="ui-monospace, monospace" '
            f'font-size="{_num(min(h * 0.72, 11))}" '
            f'fill="{_TEXT}" aria-hidden="true">{pct_int}%</text>'
        )
    return open_tag + "".join(body) + "</svg>"


# ===========================================================================
# 3. DONUT — headline audit % ring
# ===========================================================================
def donut(
    pct: Union[int, float],
    label: Optional[str] = None,
    size: float = 96,
    *,
    status: Optional[str] = None,
    thickness: Optional[float] = None,
    center_text: Optional[str] = None,
    sublabel: Optional[str] = None,
    track: Optional[str] = None,
) -> str:
    """Return a donut/ring gauge for a single percentage (the headline audit %).

    Args:
      pct: 0..100 (clamped). Drives the swept arc.
      label: REQUIRED-style accessible name -> aria-label + <title>
             (defaults to "<center_text> — <pct>%").
      size: square SVG side in px.
      status: optional 5-state role driving the arc colour; if omitted, the colour is
              chosen by threshold (>=90 pass, >=70 warn, else fail) so an audit ring is
              cued by colour AND the inline glyph — never colour alone.
      thickness: ring stroke width (defaults to ~14% of size).
      center_text: big centred label (defaults to "<pct>%").
      sublabel: small muted line under the center text.
      track: ring-track colour (defaults to surface-2).

    The arc uses ``stroke-dasharray`` over a circle (cap rounded), rotated -90deg so it
    starts at 12 o'clock and sweeps clockwise. A status GLYPH is rendered alongside the
    percentage so the gauge is legible without colour perception.
    """
    size = float(size)
    try:
        p = float(pct)
    except (TypeError, ValueError):
        p = 0.0
    if p != p:
        p = 0.0
    p = max(0.0, min(100.0, p))
    pct_int = int(round(p))

    if status:
        role = str(status).strip().lower()
        if role not in STATUS_GLYPHS:
            role = "info"
    else:
        role = "pass" if p >= 90 else ("warn" if p >= 70 else "fail")
    arc_color = _status_fill(role)
    glyph = STATUS_GLYPHS.get(role, "")

    if center_text is None:
        center_text = f"{pct_int}%"
    if label is None:
        base = sublabel or center_text
        label = f"{escape_text(base)}: {pct_int}% ({role})"

    if thickness is None:
        thickness = max(4.0, size * 0.14)
    if track is None:
        track = _SURFACE2

    cx = cy = size / 2.0
    r = (size - thickness) / 2.0 - 1  # -1 so the stroke doesn't clip the viewBox
    if r <= 0:
        r = max(1.0, size / 3.0)
    circ = 2 * 3.141592653589793 * r
    dash = circ * (p / 100.0)
    gap_len = circ - dash

    open_tag, _tid = _svg_open(size, size, label, extra_class="hub-donut")
    body: list[str] = [
        # track
        f'<circle cx="{_num(cx)}" cy="{_num(cy)}" r="{_num(r)}" '
        f'fill="none" stroke="{track}" stroke-width="{_num(thickness)}" aria-hidden="true"/>',
    ]
    if p > 0:
        body.append(
            f'<circle cx="{_num(cx)}" cy="{_num(cy)}" r="{_num(r)}" '
            f'fill="none" stroke="{arc_color}" stroke-width="{_num(thickness)}" '
            f'stroke-linecap="round" '
            f'stroke-dasharray="{_num(dash)} {_num(gap_len)}" '
            f'transform="rotate(-90 {_num(cx)} {_num(cy)})" aria-hidden="true"/>'
        )

    # Centre text: glyph + percent on one line; optional muted sublabel under it.
    big_y = cy if sublabel is None else cy - size * 0.06
    big_font = size * 0.26
    centre = f"{glyph} {escape_text(center_text)}".strip() if glyph else escape_text(center_text)
    body.append(
        f'<text x="{_num(cx)}" y="{_num(big_y)}" text-anchor="middle" '
        f'dominant-baseline="central" font-family="ui-monospace, monospace" '
        f'font-weight="600" font-size="{_num(big_font)}" fill="{_TEXT}" '
        f'aria-hidden="true">{centre}</text>'
    )
    if sublabel is not None:
        body.append(
            f'<text x="{_num(cx)}" y="{_num(cy + size * 0.16)}" text-anchor="middle" '
            f'dominant-baseline="central" font-family="ui-monospace, monospace" '
            f'font-size="{_num(size * 0.11)}" fill="{_MUTED}" '
            f'aria-hidden="true">{escape_text(sublabel)}</text>'
        )

    return open_tag + "".join(body) + "</svg>"


# ===========================================================================
# 4. HEATMAP — rect-grid (deploy calendar / FEATURES status matrix)
# ===========================================================================
def heatmap(
    grid: Sequence[Sequence[object]],
    cell: float = 12,
    *,
    label: str = "status matrix",
    gap: float = 2.0,
    radius: float = 2.0,
    pad: float = 1.0,
    cols: Optional[int] = None,
    titles: Optional[Sequence[Sequence[object]]] = None,
) -> str:
    """Return a rect-grid heatmap (deploy calendar / FEATURES status matrix).

    ``grid`` is a 2-D sequence of *cells*. Each cell may be:
      * a status string from the 5-state vocab ("pass"/"warn"/"fail"/"info"/"stale"),
      * ``None`` / "" -> an empty (surface-2) slot,
      * a mapping ``{"status": <role>, "title": <hover text>}`` for a per-cell tooltip,
      * a number in ``0..1`` -> mapped to an accent intensity (calendar-style density),
      * any other value -> rendered as an empty slot (fail-inert, never crashes).

    A ragged (jagged) ``grid`` is fine — rows can differ in length. Pass ``cols`` to force
    a fixed column count (cells beyond it wrap to a new row is NOT done; ``cols`` only sets
    the layout width for short rows). ``titles`` is an optional parallel 2-D sequence of
    hover strings (overrides per-cell title) for calendar dates etc.

    Whole grid is one ``role="img"`` named by ``label``; each filled cell carries a
    ``<title>`` so a sighted hover and the SR tree both get the per-cell meaning. Status is
    a fill role here, but because the hub ALWAYS pairs the matrix with a textual legend +
    per-cell title, meaning never rests on colour alone.
    """
    cell = float(cell)
    gap = float(gap)
    radius = float(radius)
    pad = float(pad)

    rows = [list(r) for r in grid] if grid else []
    n_rows = len(rows)
    n_cols = cols if cols is not None else (max((len(r) for r in rows), default=0))
    n_cols = max(0, int(n_cols))

    w = pad * 2 + (n_cols * cell) + (gap * max(0, n_cols - 1)) if n_cols else pad * 2
    h = pad * 2 + (n_rows * cell) + (gap * max(0, n_rows - 1)) if n_rows else pad * 2
    w = max(w, 1.0)
    h = max(h, 1.0)

    open_tag, _tid = _svg_open(w, h, label, extra_class="hub-heatmap")
    body: list[str] = []

    def _cell_fill_title(value: object) -> tuple[str, str]:
        """Return (fill, title_text) for one cell value."""
        # Mapping form: {"status": ..., "title": ...}
        if isinstance(value, Mapping):
            st = value.get("status")
            ti = value.get("title")
            fill = _status_fill(st) if st is not None else _SURFACE2
            if ti is None:
                glyph = STATUS_GLYPHS.get(str(st).lower(), "") if st is not None else ""
                ti = f"{glyph} {st}".strip() if st is not None else ""
            return fill, str(ti)
        # Numeric density in 0..1 -> accent intensity.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                f = float(value)
            except (TypeError, ValueError):
                return _SURFACE2, ""
            if f != f:
                return _SURFACE2, ""
            f = max(0.0, min(1.0, f))
            if f <= 0:
                return _SURFACE2, "0"
            # opacity-modulated accent via a wrapping rect alpha; encode as rgba-ish
            # using fill-opacity on the rect (handled at draw time). Signal via title.
            return f"__accent__{_num(f)}", _num(f)
        # String status role.
        if isinstance(value, str):
            key = value.strip().lower()
            if key in _STATUS_TOKEN:
                glyph = STATUS_GLYPHS.get(key, "")
                return _status_fill(key), f"{glyph} {value}".strip()
            if key == "":
                return _SURFACE2, ""
            # Unknown string -> inert slot but keep the raw text as a title.
            return _SURFACE2, escape_text(value)
        # None / anything else -> empty slot.
        return _SURFACE2, ""

    for ri, row in enumerate(rows):
        y = pad + ri * (cell + gap)
        for ci in range(n_cols):
            value = row[ci] if ci < len(row) else None
            x = pad + ci * (cell + gap)
            fill, title = _cell_fill_title(value)

            # External titles override.
            if titles is not None and ri < len(titles) and ci < len(titles[ri]):
                ext = titles[ri][ci]
                if ext not in (None, ""):
                    title = str(ext)

            opacity_attr = ""
            if isinstance(fill, str) and fill.startswith("__accent__"):
                inten = fill[len("__accent__"):]
                fill = _ACCENT
                opacity_attr = f' fill-opacity="{escape_attr(inten)}"'

            title_el = f"<title>{escape_text(title)}</title>" if title else ""
            body.append(
                f'<rect x="{_num(x)}" y="{_num(y)}" '
                f'width="{_num(cell)}" height="{_num(cell)}" '
                f'rx="{_num(radius)}" ry="{_num(radius)}" '
                f'fill="{fill}"{opacity_attr} '
                f'stroke="{_BORDER}" stroke-width="0.5" aria-hidden="true">'
                f'{title_el}</rect>'
            )

    return open_tag + "".join(body) + "</svg>"
