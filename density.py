"""Overall-table density, rendered with the existing Pillow chart utilities."""
import io
import math
from statistics import median
from charts import _text
from visuals import font as _font, PAPER, INK, MUTED, SERIES, TINTS, chart_header


def plural(n, forms):
    n = abs(n)
    return forms[2] if 11 <= n % 100 <= 14 else forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def teams(n):
    return f"{n} {plural(n, ('команда', 'команды', 'команд'))}"


def points(n):
    return f"{n} {plural(n, ('очко', 'очка', 'очков'))}"


def group_totals(totals):
    """Find the longest run of distinct totals with gaps <= rounded-up median positive gap.

    Keep the lower tail together; split the upper tail at its largest gap only
    when larger than that threshold. With no run of 3 distinct totals, split
    only at unusually large gaps (> twice the median), at most three times.
    Equal totals always remain together. This is a descriptive heuristic.
    """
    values = sorted(totals)
    unique = sorted(set(values))
    if not values:
        return []
    gaps = [b-a for a, b in zip(unique, unique[1:])]
    threshold = math.ceil(median(gaps)) if gaps else 0
    runs, start = [], 0
    for i, gap in enumerate(gaps):
        if gap > threshold:
            runs.append(unique[start:i+1])
            start = i+1
    runs.append(unique[start:])
    core = max(runs, key=lambda r: (len(r), sum(v in r for v in values), -r[0]))
    cuts = []
    if len(core) >= 3 and len(core) < len(unique):
        if core[0] > unique[0]:
            cuts.append(core[0])
        upper = [v for v in unique if v > core[-1]]
        if upper:
            cuts.append(upper[0])
            candidates = [(b-a, b) for a,b in zip(upper, upper[1:]) if b-a > threshold]
            if candidates:
                cuts.append(max(candidates)[1])
    else:
        cuts = [b for _,b in sorted(((b-a,b) for a,b in zip(unique,unique[1:]) if b-a > 2*threshold), reverse=True)[:3]]
    partitions = []
    for value in values:
        index = sum(value >= c for c in cuts)
        while len(partitions) <= index:
            partitions.append([])
        partitions[index].append(value)
    names = {1: ['Вся лига'], 2: ['Нижняя часть', 'Лидеры'],
             3: ['Нижняя часть', 'Середина', 'Лидеры'],
             4: ['Нижняя часть', 'Плотная середина', 'Преследователи', 'Лидеры']}[len(partitions)]
    return [dict(name=name, totals=v, count=len(v), low=min(v), high=max(v),
                 spread=max(v)-min(v), max_gap=max((b-a for a,b in zip(v,v[1:])), default=0))
            for name,v in zip(names, partitions)]


def dot_positions(totals, axis_low, axis_high, left=100, right=1500):
    """Deterministic collision offsets; x is always the exact linear encoding."""
    placed = []
    for value in sorted(totals):
        x = left + (value-axis_low)/(axis_high-axis_low)*(right-left)
        lane = 0
        while True:
            offset = 0 if lane == 0 else ((lane+1)//2)*26*(1 if lane % 2 else -1)
            if all(abs(x-px) >= 25 or abs(offset-py) >= 25 for px,py in placed):
                break
            lane += 1
        placed.append((x, offset))
    return placed


def render_density(totals, synced_at, provisional=False):
    from PIL import Image, ImageDraw
    if not totals:
        raise ValueError('Empty overall standings')
    groups = group_totals(totals)
    span = max(totals)-min(totals)
    raw = max(span/8, 1)
    magnitude = 10**math.floor(math.log10(raw))
    step = next(v*magnitude for v in (1,2,5,10) if v*magnitude >= raw)
    low = math.floor((min(totals)-step*.5)/step)*step
    high = math.ceil((max(totals)+step*.5)/step)*step
    coords = dot_positions(totals, low, high)
    extra = max(0, max(abs(y) for _,y in coords)-80)
    height = 1120 + 2*extra
    image = Image.new('RGB', (1600,height), PAPER)
    draw = ImageDraw.Draw(image)
    ink, muted = INK, MUTED
    small, body, heading = _font(22), _font(27), _font(25,True)
    def text(x,y,s,font=body,color=ink,anchor=None):
        _text(draw,(x,y),s,font,color,anchor)
    def x(v):
        return 100+(v-low)/(high-low)*1400
    dense = next((g for i,g in enumerate(groups) if 0<i<len(groups)-1 and g['count'] >= len(totals)/2 and g['spread'] > 0),None)
    state = 'ПРЕДВАРИТЕЛЬНО' if provisional else 'ОБЩАЯ ТАБЛИЦА'
    chart_header(draw, 1600, f'ELIMAI / МИНИ-ЛИГА FPL / {state}',
                 'Тесно в середине' if dense else 'Насколько плотна таблица?',
                 f"{teams(dense['count'])} · разброс — {points(dense['spread'])}" if dense else
                 'Распределение команд по общей сумме очков', title_size=80)
    centre, axis = 430+extra, 580+2*extra
    fills = list(reversed(TINTS[:len(groups)]))
    colors = list(reversed(SERIES[:len(groups)]))
    for g,fill in zip(groups,fills):
        pad = min(12,1400/(high-low)*step*.12)
        draw.rectangle((x(g['low'])-pad,330,x(g['high'])+pad,axis+49),fill=fill)
    for value,(px,offset) in zip(sorted(totals),coords):
        cy = centre+offset
        color = next(color for g,color in zip(groups,colors) if g['low']<=value<=g['high'])
        draw.ellipse((px-10,cy-10,px+10,cy+10),fill=color,outline=PAPER,width=2)
    draw.line((100,axis,1500,axis),fill=muted,width=2)
    for tick in range(low, high+1, step):
        draw.line((x(tick),axis,x(tick),axis+9),fill=muted,width=2)
        text(x(tick),axis+19,str(tick),small,muted,'ma')
    text(1500,axis+68,'ВСЕГО ОЧКОВ',small,muted,'ra')
    bracket_y = axis+120
    col_width = 1440/len(groups)
    for i,g in enumerate(groups):
        a,b=x(g['low']),x(g['high'])
        draw.line((a,bracket_y-8,a,bracket_y,b,bracket_y,b,bracket_y-8),fill=muted,width=2)
        cx=80+col_width*(i+.5)
        draw.line(((a+b)/2,bracket_y+5,cx,bracket_y+35),fill='#C8C5BD',width=1)
        text(cx,bracket_y+52,g['name'].upper(),heading,colors[i],'ma')
        text(cx,bracket_y+94,f"{teams(g['count'])} · {g['low']}–{points(g['high'])}",small,muted,'ma')
        lines = ([f"Всего {points(g['low'])}.", 'Команды идут вровень.'] if g['spread']==0 and g['count']>1 else
                 ['Одна команда в этой зоне.'] if g['count']==1 else
                 [f"Разброс — {points(g['spread'])}.","Максимальный разрыв",f"между соседями: {points(g['max_gap'])}."])
        if i == len(groups)-1 and g['count'] == 2:
            lines=['Первое и второе места', f"разделяет {points(g['spread'])}."]
        if len(groups)==4 and i==2:
            lines=[f"Внутри — {points(g['spread'])}.",'До второго места —',f"ещё {points(sorted(totals)[-2]-g['high'])}."]
        for j,line in enumerate(lines):
            text(cx,bracket_y+139+j*32,line,small,ink,'ma')
    footer=height-145
    draw.line((80,footer,1520,footer),fill='#CCC9C0',width=1)
    text(80,footer+20,'Каждая точка — одна команда. По горизонтали — общая сумма очков.',small,muted)
    text(80,footer+53,'Равные суммы расположены по вертикали. Цветные зоны — группы команд.',small,muted)
    state='предварительные данные' if provisional else 'общая таблица'
    text(80,footer+96,f'Источник: Fantasy Premier League · {state} · {synced_at} UTC',small,muted)
    output=io.BytesIO()
    output.name='elimai-league-density.png'
    image.save(output,'PNG',optimize=True)
    output.seek(0)
    return output
