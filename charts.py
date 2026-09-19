"""Deterministic, database-backed editorial charts for Telegram."""
from __future__ import annotations

import io
import math
from pathlib import Path

from data import rows


async def chart_spec(db, gw: int, requested: str, scope: str) -> dict:
    """Return a small, fully sourced chart specification from stored league rows."""
    status = await rows(
        db,
        "SELECT COALESCE(r.provisional,0) provisional,s.synced_at "
        "FROM sync_runs s LEFT JOIN result_status r USING(gw_id) WHERE s.gw_id=?",
        (gw,),
    )
    if not status:
        raise ValueError("Gameweek is not synced")

    coverage = await rows(db, "SELECT gw_id FROM sync_runs WHERE gw_id<=? ORDER BY gw_id", (gw,))
    complete = [r["gw_id"] for r in coverage] == list(range(1, gw + 1))
    if requested not in ("latest", "history") or scope not in ("top10", "all"):
        raise ValueError("Unknown chart variant")

    common = {
        "gw": gw,
        "provisional": bool(status[0]["provisional"]),
        "synced_at": status[0]["synced_at"],
    }
    if requested == "history":
        if not complete or gw < 2:
            raise ValueError("Not enough continuous history")
        limit = " LIMIT 10" if scope == "top10" else ""
        leaders = await rows(db, "SELECT entry_id FROM standings WHERE gw_id=? "
                             "ORDER BY total_points DESC,rank,entry_id" + limit, (gw,))
        ids = [r["entry_id"] for r in leaders]
        placeholders = ",".join("?" for _ in ids)
        series = await rows(
            db,
            f"SELECT s.entry_id,m.entry_name,s.gw_id,s.rank,s.total_points "
            f"FROM standings s JOIN managers m USING(entry_id) "
            f"WHERE s.gw_id<=? AND s.entry_id IN ({placeholders}) "
            f"ORDER BY s.entry_id,s.gw_id",
            (gw, *ids),
        )
        return {**common, "kind": "history", "scope": scope, "rows": series,
                "manager_count": len(leaders)}

    limit = " LIMIT 10" if scope == "top10" else ""
    latest = await rows(
        db,
        "SELECT m.entry_name,s.event_points,s.rank,s.total_points "
        "FROM standings s JOIN managers m USING(entry_id) WHERE s.gw_id=? "
        "ORDER BY s.event_points DESC,s.entry_id" + limit,
        (gw,),
    )
    all_scores = await rows(db, "SELECT event_points FROM standings WHERE gw_id=? ORDER BY event_points", (gw,))
    if not latest:
        raise ValueError("No standings for gameweek")
    scores = [r["event_points"] for r in all_scores]
    middle = len(scores) // 2
    median = scores[middle] if len(scores) % 2 else (scores[middle - 1] + scores[middle]) / 2
    return {**common, "kind": "latest", "scope": scope, "rows": latest,
            "median": median, "manager_count": len(scores)}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = (["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
             if bold else ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"])
    names += (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
              ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for name in names:
        if Path(name).exists():
            try:
                return ImageFont.truetype(name, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _text(draw, xy, text, font, fill, anchor=None):
    draw.text(xy, str(text), font=font, fill=fill, anchor=anchor)


def render_chart(spec: dict) -> io.BytesIO:
    """Render a 1600x1000 PNG. No model-generated numbers or claims are used."""
    from PIL import Image, ImageDraw

    width = 1600
    height = max(1000, 400 + len(spec["rows"]) * 52) if spec["kind"] == "latest" else 1100
    paper, ink, muted, grid, red = "#F3F0E9", "#171717", "#6C6964", "#D4CFC5", "#D71920"
    image = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(image)
    title, deck, label, small = _font(62, True), _font(29), _font(24, True), _font(20)

    draw.rectangle((70, 52, 1530, 64), fill=ink)
    state = "LIVE / PROVISIONAL" if spec["provisional"] else "FINAL"
    _text(draw, (70, 92), f"ELIMAI LEAGUE  /  GW{spec['gw']}  /  {state}", label, red)

    if spec["kind"] == "latest":
        _text(draw, (70, 142), "Who owned the gameweek?", title, ink)
        subject = "Top 10" if spec["scope"] == "top10" else "All managers"
        _text(draw, (70, 222), f"{subject} • whole-league median shown as the benchmark", deck, muted)
        rows_ = spec["rows"]
        left, top, right, bottom = 390, 318, 1490, height - 128
        max_score = max(r["event_points"] for r in rows_)
        axis_max = max(10, int(math.ceil(max_score / 10)) * 10)
        median_x = left + (right - left) * spec["median"] / axis_max
        draw.line((median_x, top - 18, median_x, bottom + 8), fill=red, width=3)
        _text(draw, (median_x, top - 28), f"MEDIAN {spec['median']:g}", small, red, "ms")
        row_h = (bottom - top) / len(rows_)
        for i, row in enumerate(rows_):
            cy = top + i * row_h + row_h / 2
            bar_right = left + (right - left) * row["event_points"] / axis_max
            _text(draw, (left - 24, cy), row["entry_name"][:28], label if i == 0 else small, ink, "rm")
            draw.rectangle((left, cy - 14, bar_right, cy + 14), fill=red if i == 0 else ink)
            _text(draw, (bar_right + 16, cy), row["event_points"], label, ink, "lm")
        _text(draw, (70, height - 87), f"{subject} of {spec['manager_count']} managers • points after transfer costs", small, muted)
    else:
        _text(draw, (70, 142), "The title race, gameweek by gameweek", title, ink)
        subject = "Current top 10" if spec["scope"] == "top10" else "Every manager"
        _text(draw, (70, 222), f"{subject} • rank 1 is the summit", deck, muted)
        data = spec["rows"]
        gameweeks = sorted({r["gw_id"] for r in data})
        by_team = {}
        for row in data:
            by_team.setdefault((row["entry_id"], row["entry_name"]), []).append(row)
        left, top, right, bottom = 120, 326, 1280, height - 150
        max_rank = max(r["rank"] for r in data)
        y_max = max(6, max_rank)
        x = lambda gw: left if len(gameweeks) == 1 else left + (right - left) * (gw - gameweeks[0]) / (gameweeks[-1] - gameweeks[0])
        y = lambda rank: top + (bottom - top) * (rank - 1) / max(1, y_max - 1)
        for rank in range(1, y_max + 1):
            yy = y(rank)
            draw.line((left, yy, right, yy), fill=grid, width=1)
            if rank == 1 or rank == y_max or rank % 5 == 0:
                _text(draw, (left - 20, yy), rank, small, muted, "rm")
        for gw in gameweeks:
            xx = x(gw)
            draw.line((xx, top, xx, bottom), fill=grid, width=1)
            _text(draw, (xx, bottom + 28), f"GW{gw}", small, muted, "ma")
        ordered = sorted(by_team.items(), key=lambda item: item[1][-1]["rank"], reverse=True)
        for (_, name), points in ordered:
            color = red if points[-1]["rank"] == 1 else (ink if spec["scope"] == "top10" else muted)
            coords = [(x(p["gw_id"]), y(p["rank"])) for p in points]
            if len(coords) > 1:
                draw.line(coords, fill=color, width=7 if color == red else (4 if spec["scope"] == "top10" else 3), joint="curve")
            for xx, yy in coords:
                draw.ellipse((xx - 6, yy - 6, xx + 6, yy + 6), fill=color)
            last = points[-1]
            _text(draw, (right + 20, y(last["rank"])), f"{name[:22]}  #{last['rank']}", label if color == red else small, color, "lm")
        _text(draw, (70, height - 87), f"{subject} by total points • historical ranks use current league membership", small, muted)

    _text(draw, (1530, height - 42), f"Source: Fantasy Premier League • synced {spec['synced_at']} UTC", small, muted, "ra")
    output = io.BytesIO()
    output.name = f"elimai-league-{spec['kind']}-{spec['scope']}-gw{spec['gw']}.png"
    image.save(output, "PNG", optimize=True)
    output.seek(0)
    return output
