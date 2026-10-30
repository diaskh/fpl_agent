"""Whole-league cumulative-points bar chart race."""
from __future__ import annotations

import io
import math
import os
import shutil
import tempfile
from pathlib import Path

from data import rows
from visuals import PAPER, INK, MUTED, SERIES, FONTS


async def race_spec(db, gw: int) -> dict:
    """Extract continuous cumulative totals for every current league manager."""
    status = await rows(
        db,
        "SELECT COALESCE(r.provisional,0) provisional,s.synced_at "
        "FROM sync_runs s LEFT JOIN result_status r USING(gw_id) WHERE s.gw_id=?",
        (gw,),
    )
    if not status:
        raise ValueError("Gameweek is not synced")
    managers = await rows(db, "SELECT entry_id,entry_name FROM managers ORDER BY entry_id")
    history = await rows(
        db,
        "SELECT s.entry_id,m.entry_name,s.gw_id,s.total_points "
        "FROM standings s JOIN managers m USING(entry_id) WHERE s.gw_id<=? "
        "ORDER BY s.gw_id,s.entry_id",
        (gw,),
    )
    synced = [r["gw_id"] for r in await rows(db, "SELECT gw_id FROM sync_runs WHERE gw_id<=? ORDER BY gw_id", (gw,))]
    if gw < 2 or synced != list(range(1, gw + 1)):
        raise ValueError("Race requires continuous synced gameweeks")
    weeks_by_manager = {m["entry_id"]: [] for m in managers}
    for row in history:
        weeks_by_manager[row["entry_id"]].append(row["gw_id"])
    for weeks in weeks_by_manager.values():
        # A late entrant is absent before their first verified snapshot. Once present,
        # every subsequent gameweek through the current one must be known.
        if not weeks or weeks[-1] != gw or weeks != list(range(weeks[0], gw + 1)):
            raise ValueError("Manager history has an internal gap")

    # Team names need to be unique DataFrame columns. Preserve normal names unless a
    # duplicate would otherwise merge two separate FPL entries.
    counts = {}
    for manager in managers:
        counts[manager["entry_name"]] = counts.get(manager["entry_name"], 0) + 1
    labels = {m["entry_id"]: (m["entry_name"] if counts[m["entry_name"]] == 1
                              else f"{m['entry_name']} ({m['entry_id']})") for m in managers}
    return {
        "gw": gw,
        "provisional": bool(status[0]["provisional"]),
        "synced_at": status[0]["synced_at"],
        "labels": labels,
        "rows": history,
    }


def render_race(spec: dict) -> io.BytesIO:
    """Render an MP4 with bar_chart_race 0.1.0 and the local FFmpeg binary."""
    matplotlib_cache = Path(tempfile.gettempdir()) / "fpl-bot-matplotlib"
    matplotlib_cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    import matplotlib
    matplotlib.use("Agg")
    import bar_chart_race as bcr
    import matplotlib.pyplot as plt
    import pandas as pd

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to render the race")
    matplotlib.rcParams["animation.ffmpeg_path"] = ffmpeg

    records = [{"gameweek": f"GW{r['gw_id']}", "team": spec["labels"][r["entry_id"]],
                "points": r["total_points"]} for r in spec["rows"]]
    frame = pd.DataFrame.from_records(records).pivot(index="gameweek", columns="team", values="points")
    frame = frame.reindex([f"GW{week}" for week in range(1, spec["gw"] + 1)])
    # Leading NaNs are intentional: bar_chart_race leaves those managers absent until
    # their first verified league snapshot, while interpolating only known transitions.

    # Add eight small value stages per gameweek transition, followed by a duplicate
    # endpoint. The duplicate creates a short editorial pause at every GW while the
    # staged values keep movement continuous. Late entrants remain absent until their
    # first real snapshot instead of being interpolated from an invented zero.
    staged_rows = [frame.iloc[0], frame.iloc[0]]
    staged_labels = [frame.index[0], frame.index[0]]
    for position in range(1, len(frame)):
        previous, current = frame.iloc[position - 1], frame.iloc[position]
        for stage in range(1, 9):
            if stage == 8:
                values = current
            else:
                alpha = stage / 8
                values = previous + (current - previous) * alpha
            staged_rows.append(values)
            staged_labels.append(frame.index[position] if stage == 8 else frame.index[position - 1])
        staged_rows.append(current)
        staged_labels.append(frame.index[position])
    frame = pd.DataFrame(staged_rows, index=staged_labels)

    from matplotlib import font_manager
    from matplotlib.colors import ListedColormap
    for filename in ('Inter.ttf', 'Oswald.ttf'):
        font_manager.fontManager.addfont(str(FONTS / filename))
    fig, ax = plt.subplots(figsize=(16, 11), dpi=100)
    fig.subplots_adjust(left=.24, right=.94, top=.76, bottom=.12)
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    ax.tick_params(colors=MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)
    state = 'ПРЕДВАРИТЕЛЬНО' if spec['provisional'] else 'ИТОГОВЫЕ ДАННЫЕ'
    fig.text(.06, .95, f"ELIMAI / МИНИ-ЛИГА FPL / {state}",
             fontfamily='Inter', fontsize=12, color=MUTED, weight='bold')
    fig.text(.06, .87, 'ГОНКА ЗА ЛИДЕРСТВОМ',
             fontfamily='Oswald', fontsize=36, color=INK, weight='bold')
    fig.text(.06, .81, 'Как менялась общая сумма очков от тура к туру',
             fontfamily='Inter', fontsize=15, color=INK)
    fig.text(.06, .035, f"Источник: Fantasy Premier League · обновлено {spec['synced_at']} UTC",
             fontfamily='Inter', fontsize=10, color=MUTED)
    ax.set_xlabel('ОБЩАЯ СУММА ОЧКОВ', color=INK, fontfamily='Inter', labelpad=15)
    # bar_chart_race applies period_length to every staged interval. Distribute a
    # 15-second target across them and derive enough frames for smooth ~30 fps motion.
    period_length = math.ceil(15_100 / (len(frame) - 1))
    steps_per_period = max(2, math.ceil(period_length * 30 / 1000))

    with tempfile.TemporaryDirectory(prefix="fpl-race-") as directory:
        destination = str(Path(directory) / "elimai-league-race.mp4")
        bcr.bar_chart_race(
            df=frame,
            filename=destination,
            orientation="h",
            sort="desc",
            n_bars=len(frame.columns),
            fixed_order=False,
            fixed_max=False,
            steps_per_period=steps_per_period,
            period_length=period_length,
            interpolate_period=False,
            label_bars=True,
            bar_size=.82,
            period_label={"x": .98, "y": .08, "ha": "right", "va": "center", "color": SERIES[0]},
            figsize=(16, 11),
            dpi=100,
            cmap=ListedColormap(SERIES),
            title=None,
            bar_label_size=10,
            tick_label_size=10,
            shared_fontdict={"family": "Inter", "weight": "normal", "color": INK},
            writer="ffmpeg",
            fig=fig,
            bar_kwargs={"alpha": .88},
        )
        plt.close(fig)
        payload = Path(destination).read_bytes()
    output = io.BytesIO(payload)
    output.name = f"elimai-league-race-gw{spec['gw']}.mp4"
    output.seek(0)
    return output
