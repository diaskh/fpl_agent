"""Deterministic, database-backed editorial charts for Telegram."""
from __future__ import annotations

import io
import math
from visuals import (font as _font, fit_text,
                     PAPER, INK, MUTED, RULE, ACCENT)

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


def _text(draw, xy, text, font, fill, anchor=None):
    draw.text(xy, str(text), font=font, fill=fill, anchor=anchor)


def render_chart(spec: dict) -> io.BytesIO:
    """Render exact stored values with the same editorial system as /graph form."""
    from PIL import Image, ImageDraw
    from visuals import chart_header, chart_footer, SERIES, BLUE

    latest = spec["kind"] == "latest"
    data = spec["rows"]
    width = 2200
    count = len(data) if latest else len({r["entry_id"] for r in data})
    height = max(1500, 530 + count * 48)
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    def text(x, y, value, size=24, color=INK, bold=False, anchor="lt"):
        draw.text((x, y), str(value), font=_font(size, bold), fill=color, anchor=anchor)
    state = "ПРЕДВАРИТЕЛЬНО" if spec["provisional"] else "ИТОГОВЫЕ ДАННЫЕ"
    subject = "Топ-10" if spec["scope"] == "top10" else "Вся мини-лига"
    chart_header(draw, width, f"ELIMAI / МИНИ-ЛИГА FPL / ТУР {spec['gw']} / {state}",
                 "Кто выиграл этот тур?" if latest else "ГОНКА ЗА ЛИДЕРСТВОМ",
                 f"{subject} · очки за тур и медиана всей лиги" if latest else
                 f"{subject} · положение в общей таблице от тура к туру")
    left, top, right, bottom = (580, 350, 2010, height-250) if latest else (165, 350, 1510, height-250)
    if latest:
        scores = [r["event_points"] for r in data] + [spec["median"], 0]
        low = min(0, math.floor(min(scores)/10)*10)
        high = max(10, math.ceil(max(scores)/10)*10)
        x = lambda v: left + (v-low)/(high-low)*(right-left)
        step = max(10, math.ceil((high-low)/100)*10)
        for tick in range(low, high+1, step):
            xx = x(tick)
            draw.line((xx, top, xx, bottom), fill=RULE, width=1)
            text(xx, bottom+22, tick, 22, MUTED, anchor="mt")
        draw.line((x(0), top, x(0), bottom), fill=MUTED, width=2)
        row_h = (bottom-top)/count
        for i, row in enumerate(data):
            cy = top+(i+.5)*row_h
            name, face = fit_text(draw, row["entry_name"], 27, left-135)
            draw.text((left-30, cy), name, font=face, fill=INK, anchor="rm")
            value = row["event_points"]
            a, b = sorted((x(0), x(value)))
            draw.rectangle((a, cy-14, b, cy+14), fill=ACCENT if value >= spec["median"] else BLUE)
            text(x(value)+(15 if value>=0 else -15), cy, value, 25, INK, True,
                 "lm" if value>=0 else "rm")
        mx = x(spec["median"])
        draw.line((mx, top-12, mx, bottom), fill=ACCENT, width=3)
        text(mx, top-33, f"МЕДИАНА {spec['median']:g}", 21, ACCENT, True, "mb")
        note = f"{subject} · в лиге {spec['manager_count']} команд · очки после учёта штрафов за трансферы"
    else:
        teams = {}
        for row in data:
            teams.setdefault(row["entry_id"], []).append(row)
        ordered = sorted(teams.values(), key=lambda rs: (rs[-1]["rank"], rs[-1]["entry_id"]))
        weeks = sorted({r["gw_id"] for r in data})
        max_rank = max(6, max(r["rank"] for r in data))
        x = lambda gw: left+(right-left)*(gw-weeks[0])/max(1, weeks[-1]-weeks[0])
        y = lambda rank: top+(bottom-top)*(rank-1)/(max_rank-1)
        for rank in range(1, max_rank+1):
            yy = y(rank)
            draw.line((left, yy, right, yy), fill=RULE, width=1)
            if rank == 1 or rank == max_rank or rank % 5 == 0:
                text(left-25, yy, rank, 22, MUTED, anchor="rm")
        for gw in weeks:
            xx = x(gw)
            draw.line((xx, top, xx, bottom), fill=RULE, width=1)
            text(xx, bottom+24, gw, 22, MUTED, anchor="mt")
        text((left+right)/2, bottom+70, "ТУР", 23, INK, True, "mt")
        text(1580, 310, "КОМАНДЫ", 27, INK, True)
        text(1580, 350, "По текущему месту в лиге", 21, MUTED)
        for i, values in enumerate(ordered):
            color = SERIES[i % len(SERIES)]
            coords = [(x(r["gw_id"]), y(r["rank"])) for r in values]
            if len(coords)>1:
                draw.line(coords, fill=color, width=4, joint="curve")
            for xx, yy in coords:
                draw.ellipse((xx-5, yy-5, xx+5, yy+5), fill=color)
            # Reference numbers keep names out of the dense plotting area.
            end_x, end_y = coords[-1]
            offset = sum(v[-1]["rank"] == values[-1]["rank"] for v in ordered[:i])
            label_x = end_x+23+offset*40
            text(label_x, end_y, i+1, 20, color, True, "lm")
            key_y = 405+i*43
            text(1580, key_y, f"{i+1:02d}", 23, color, True)
            name, face = fit_text(draw, f"{values[-1]['entry_name']}  ·  #{values[-1]['rank']}", 25, 480)
            draw.text((1630, key_y), name, font=face, fill=INK, anchor="lt")
        note = "Выше — лучше · исторические места восстановлены по текущему составу мини-лиги"
    chart_footer(draw, width, height, note,
                 f"Источник: Fantasy Premier League · обновлено {spec['synced_at']} UTC")
    output = io.BytesIO()
    output.name = f"elimai-league-{spec['kind']}-{spec['scope']}-gw{spec['gw']}.png"
    image.save(output, "PNG", optimize=True)
    output.seek(0)
    return output
