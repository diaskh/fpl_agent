"""Database-backed season strength versus recent form, rendered with Pillow."""
import io
import math
from statistics import mean
from data import rows
from visuals import font, headline, fit_text, PAPER, INK, MUTED

async def form_spec(db, gw):
    """Centre all current teams on the same cohort's season and recent means.

    Require every current team to have all three recent gameweeks. Missing
    history must never become zero or silently remove a team from the chart.
    """
    if gw < 3:
        raise ValueError('Form chart requires three gameweeks')
    status = await rows(db, 'SELECT s.gw_id,s.synced_at,COALESCE(r.provisional,0) provisional FROM sync_runs s LEFT JOIN result_status r USING(gw_id) WHERE s.gw_id BETWEEN ? AND ? ORDER BY s.gw_id', (gw - 2, gw))
    if [s['gw_id'] for s in status] != list(range(gw - 2, gw + 1)):
        raise ValueError('Recent gameweeks are not synced')
    teams = await rows(db, 'SELECT m.entry_name,s.entry_id,s.total_points,SUM(h.event_points) recent,COUNT(h.gw_id) n FROM standings s JOIN managers m USING(entry_id) LEFT JOIN standings h ON h.entry_id=s.entry_id AND h.gw_id BETWEEN ? AND ? WHERE s.gw_id=? GROUP BY s.entry_id ORDER BY s.total_points DESC,s.rank,s.entry_id', (gw - 2, gw, gw))
    if not teams or any((t['n'] != 3 for t in teams)):
        raise ValueError('Incomplete recent history for current league teams')
    season_mean = mean((t['total_points'] for t in teams))
    recent_mean = mean((t['recent'] for t in teams))
    return {'gw': gw, 'start_gw': gw - 2, 'rows': teams, 'season_mean': season_mean, 'recent_mean': recent_mean, 'provisional': any((s['provisional'] for s in status)), 'synced_at': status[-1]['synced_at']}

def render_form(spec):
    """Keep point coordinates exact; numbered callouts resolve crowded labels."""
    from PIL import Image, ImageDraw
    r = spec['rows']
    gw = spec['gw']
    a, b = (spec['season_mean'], spec['recent_mean'])
    W, H = (2200, max(1640, 740 + len(r) * 43))
    im = Image.new('RGB', (W, H), PAPER)
    d = ImageDraw.Draw(im)

    def t(x, y, s, size=24, fill=INK, bold=False, anchor='lt'):
        d.text((x, y), s, font=font(size, bold), fill=fill, anchor=anchor)
    state = 'ПРЕДВАРИТЕЛЬНО' if spec['provisional'] else 'ИТОГОВЫЕ ДАННЫЕ'
    t(90, 55, f'ELIMAI  /  МИНИ-ЛИГА FPL  /  ТУР {gw}  /  {state}', 24, MUTED, True)
    headline(d, (85, 105), 'СИЛА СЕЗОНА × ТЕКУЩАЯ ФОРМА', 85, 2020)
    t(90, 218, 'Кто удерживает темп, кто возвращается в борьбу, а кто начинает сдавать позиции', 29)
    L, R, T, B = (165, 1510, 360, H - 310)
    xm = max(50, math.ceil(max((abs(x['total_points'] - a) for x in r)) / 50) * 50 + 25)
    ym = max(20, math.ceil(max((abs(x['recent'] - b) for x in r)) / 20) * 20 + 10)

    def px(x):
        return L + (x + xm) / (2 * xm) * (R - L)

    def py(y):
        return B - (y + ym) / (2 * ym) * (B - T)
    CX, CY = (px(0), py(0))
    colors = ['#B76F35', '#267A68', '#8A637B', '#46738D']
    d.rectangle((L, T, CX, CY), fill='#EEE7D9')
    d.rectangle((CX, T, R, CY), fill='#E2ECE4')
    d.rectangle((L, CY, CX, B), fill='#EBE6EA')
    d.rectangle((CX, CY, R, B), fill='#E4E9EC')
    for x in range(-int(xm // 50) * 50, int(xm) + 1, 50):
        p = px(x)
        d.line((p, T, p, B), fill='#D9D6D0', width=1)
        t(p, B + 20, f'{x:+d}' if x else '0', 22, MUTED, anchor='mt')
    for y in range(-int(ym // 20) * 20, int(ym) + 1, 20):
        p = py(y)
        d.line((L, p, R, p), fill='#D9D6D0', width=1)
        t(L - 20, p, f'{y:+d}' if y else '0', 22, MUTED, anchor='rm')
    d.line((CX, T, CX, B), fill='#777B75', width=3)
    d.line((L, CY, R, CY), fill='#777B75', width=3)
    for x, y, s, col in [(L + 25, T + 23, 'НАБИРАЮТ ОБОРОТЫ', colors[0]), (CX + 25, T + 23, 'ЛИДЕРЫ НА ХОДУ', colors[1]), (L + 25, B - 50, 'НУЖНА ПЕРЕЗАГРУЗКА', colors[2]), (CX + 25, B - 50, 'ТЕРЯЮТ ТЕМП', colors[3])]:
        t(x, y, s, 24, col, True)
    t(L, T - 49, f"ФОРМА: очки за туры {spec['start_gw']}–{gw} относительно среднего", 24, MUTED)
    t((L + R) / 2, B + 69, f'СИЛА СЕЗОНА: очки за туры 1–{gw} относительно среднего', 24, INK, True, 'mt')
    placed = []
    for i, v in enumerate(r, 1):
        x, y = (v['total_points'] - a, v['recent'] - b)
        col = colors[1 if x >= 0 and y >= 0 else 0 if y >= 0 else 3 if x >= 0 else 2]
        xx, yy = (px(x), py(y))
        d.ellipse((xx - 8, yy - 8, xx + 8, yy + 8), fill=col, outline=PAPER, width=2)
        best = None
        for radius in (25, 42, 62, 84, 106):
            for deg in (-45, 45, -135, 135, 0, 90, 180, 270):
                q = math.radians(deg)
                nx, ny = (xx + radius * math.cos(q), yy + radius * math.sin(q))
                if not L + 20 < nx < R - 20 or not T + 85 < ny < B - 85:
                    continue
                if all(((nx - u) ** 2 + (ny - vv) ** 2 > 43 ** 2 for u, vv in placed)):
                    best = (nx, ny)
                    break
            if best:
                break
        nx, ny = best or (xx + 25, yy - 25)
        placed.append((nx, ny))
        d.line((xx, yy, nx, ny), fill=col, width=2)
        d.ellipse((nx - 18, ny - 18, nx + 18, ny + 18), fill=col)
        t(nx, ny, str(i), 19, '#FFFFFF', True, 'mm')
    t(1580, 311, 'КОМАНДЫ', 27, INK, True)
    t(1580, 350, 'По общему числу очков', 20, MUTED)
    for i, v in enumerate(r, 1):
        yy = 402 + (i - 1) * 43
        t(1580, yy, f'{i:02d}', 23, MUTED, True)
        name, face = fit_text(d, v['entry_name'], 25, 475)
        d.text((1631, yy), name, font=face, fill=INK, anchor='lt')
    d.line((90, H - 180, 2110, H - 180), fill='#CBC8C1', width=2)
    t(90, H - 150, f'Ноль = среднее по {len(r)} командам: сезон {a:.1f} очк. · последние 3 тура {b:.1f} очк.'.replace('.', ','), 25, INK)
    t(90, H - 103, 'Каждая точка — команда. Номера соответствуют списку справа. Положительные значения — выше среднего.', 22, MUTED)
    t(90, H - 63, f"Источник: Fantasy Premier League · обновлено {spec['synced_at']} UTC", 21, MUTED)
    output = io.BytesIO()
    output.name = f'elimai-league-form-gw{gw}.png'
    im.save(output, 'PNG', optimize=True)
    output.seek(0)
    return output
