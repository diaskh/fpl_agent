"""Persistent, randomly drawn knockout cups and deterministic Pillow graphics."""
import asyncio
import copy
import io
import json
import logging
import random
from datetime import datetime, timezone

from telegram.error import BadRequest, Forbidden, RetryAfter

from visuals import (font as cup_font, fit_text, headline, tracked_text,
                     PAPER, INK, MUTED, QUIET, ACCENT, RULE, CARD)
from data import rows


def round_name(count):
    return {1: 'Финал', 2: 'Полуфиналы', 4: 'Четвертьфиналы',
            8: '1/8 финала', 16: '1/16 финала'}.get(count, 'Предварительный раунд')


def draw_cup(members, start, rng=None):
    rng = rng or random.SystemRandom()
    ids = [m['entry'] for m in members]
    if not 2 <= len(ids) <= 32 or len(set(ids)) != len(ids):
        raise ValueError('Кубок поддерживает от 2 до 32 разных команд.')
    rng.shuffle(ids)
    priority = ids.copy()
    rng.shuffle(priority)
    base = 1 << (len(ids).bit_length() - 1)
    preliminary = len(ids) - base
    stages = []
    if preliminary:
        stages.append({'gw': start, 'preliminary': True, 'matches': [
            {'a': ids[2*i], 'b': ids[2*i+1], 'label': f'П{i+1}'}
            for i in range(preliminary)]})
    slots = [{'from': [0, i]} for i in range(preliminary)] + [
        {'entry': entry} for entry in ids[2*preliminary:]]
    # Pair each preliminary winner with a bye recipient when numbers allow it.
    if preliminary and preliminary <= base // 2:
        winners, byes = slots[:preliminary], slots[preliminary:]
        slots = [slot for pair in zip(winners, byes[:preliminary]) for slot in pair] + byes[preliminary:]
    level = 0
    while len(slots) > 1:
        stage_index = len(stages)
        matches = [{'slots': slots[i:i+2], 'label': f'{level+1}.{i//2+1}'}
                   for i in range(0, len(slots), 2)]
        stages.append({'gw': start + stage_index, 'preliminary': False, 'matches': matches})
        slots = [{'from': [stage_index, i]} for i in range(len(matches))]
        level += 1
    return {'names': {str(m['entry']): m['entry_name'] for m in members},
            'priority': priority, 'stages': stages}


def participants(state, match):
    if 'slots' not in match:
        return [match['a'], match['b']]
    return [s.get('entry') if 'entry' in s else
            state['stages'][s['from'][0]]['matches'][s['from'][1]].get('winner')
            for s in match['slots']]


def slot_name(state, slot):
    if 'entry' in slot:
        return state['names'][str(slot['entry'])]
    stage, index = slot['from']
    source = state['stages'][stage]['matches'][index]
    if source.get('winner') is not None:
        return state['names'][str(source['winner'])]
    return f"Победитель {source['label']}"


def settle(state, index, scores):
    result = copy.deepcopy(state)
    priority = {entry: i for i, entry in enumerate(state['priority'])}
    for match in result['stages'][index]['matches']:
        a, b = participants(result, match)
        if a is None or b is None or a not in scores or b not in scores:
            raise ValueError('Нет подтверждённых очков всех участников раунда.')
        match.update(a=a, b=b, scores=[scores[a], scores[b]],
                     winner=max((a, b), key=lambda entry: (scores[entry], -priority[entry])))
    result['stages'][index]['done'] = True
    return result


def summary(state, index):
    stage = state['stages'][index]
    title = 'Предварительный раунд' if stage['preliminary'] else round_name(len(stage['matches']))
    lines = [f"Кубок мини-лиги · {title} · GW{stage['gw']}"]
    for match in stage['matches']:
        a, b = match['a'], match['b']
        x, y = match['scores']
        tie = ' (по жребию при ничьей)' if x == y else ''
        lines.append(f"{state['names'][str(a)]} {x}:{y} {state['names'][str(b)]}\n"
                     f"→ {state['names'][str(match['winner'])]}{tie}")
    if index == len(state['stages']) - 1:
        winner = stage['matches'][0]['winner']
        lines.append(f"Чемпион: {state['names'][str(winner)]}!")
    else:
        lines.append(f"Следующий раунд: GW{state['stages'][index+1]['gw']}")
    return lines


def text_chunks(lines, limit=3500):
    chunks, current = [], ''
    for line in lines:
        if len(current) + len(line) + 2 > limit:
            chunks.append(current)
            current = ''
        current += ('\n\n' if current else '') + line
    if current:
        chunks.append(current)
    return chunks


def announcement(state):
    stages = state['stages']
    lines = [f"Кубок мини-лиги · жеребьёвка\n{len(state['names'])} команд · "
             f"GW{stages[0]['gw']}–GW{stages[-1]['gw']}",
             'Один тур на раунд. Очки FPL за тур минус штрафы за трансферы. '
             'Результаты фиксируются после проверки тура FPL. '
             'При равенстве проходит команда выше в заранее случайно определённом списке ниже. '
             'Участники и сетка зафиксированы; повторной жеребьёвки нет.']
    if stages[0]['preliminary']:
        playing = {e for m in stages[0]['matches'] for e in participants(state, m)}
        lines.append('Без предварительного раунда: ' + ', '.join(
            name for entry, name in state['names'].items() if int(entry) not in playing))
    lines.append('Приоритет при ничьей (жребий):\n' + '\n'.join(
        f"{i}. {state['names'][str(entry)]}" for i, entry in enumerate(state['priority'], 1)))
    return lines


def render_cup(state, preliminary=False):
    from PIL import Image, ImageDraw
    paper, ink, muted, accent, line = PAPER, INK, MUTED, ACCENT, RULE
    stages = state['stages'][:1] if preliminary else [s for s in state['stages'] if not s['preliminary']]
    first_count = len(stages[0]['matches'])
    width = 1600
    height = max(1000, 370 + first_count * (210 if preliminary else 160))
    image = Image.new('RGB', (width, height), paper)
    d = ImageDraw.Draw(image)
    def text(x, y, value, size=24, color=ink, bold=False, max_width=None, anchor='lt', display=False):
        value, font = fit_text(d, value, size, max_width or 1500, bold, display)
        d.text((x, y), value, font=font, fill=color, anchor=anchor)
    d.rectangle((50, 36, width-50, 44), fill=ink)
    tracked_text(d, (50, 65), 'ELIMAI LEAGUE / КУБОК', cup_font(20, True), accent)
    headline(d, (50, 108), 'КУБОК МИНИ-ЛИГИ', size=90)
    tracked_text(d, (50, 226), 'Предварительный раунд · путь в основную сетку' if preliminary else
                 'Основная сетка · результаты сохраняются на всём пути', cup_font(22), spacing=1)
    top, bottom = 350, height-160
    if preliminary:
        for i, match in enumerate(stages[0]['matches']):
            y = top + i * (bottom-top) / first_count
            d.line((55, y-20, 1545, y-20), fill=line, width=1)
            text(55, y, f"{match['label']} · GW{stages[0]['gw']}", 23, accent, True)
            a, b = participants(state, match)
            text(55, y+35, state['names'][str(a)], 28, muted if 'winner' in match and match['winner'] != a else ink, bold=match.get('winner') == a, max_width=480)
            text(55, y+77, state['names'][str(b)], 28, muted if 'winner' in match and match['winner'] != b else ink, bold=match.get('winner') == b, max_width=480)
            if 'scores' in match:
                text(550, y+45, f"{match['scores'][0]} : {match['scores'][1]}", 32, accent, True)
            # Locate the actual downstream opponent, rather than infer by name.
            target = next(m for m in state['stages'][1]['matches']
                          if any(s.get('from') == [0, i] for s in m['slots']))
            other = next(s for s in target['slots'] if s.get('from') != [0, i])
            text(780, y+15, f"→ Матч {target['label']} · GW{state['stages'][1]['gw']}", 25, accent, True)
            text(780, y+55, 'Победитель встретится с:', 24, muted)
            text(780, y+92, slot_name(state, other), 28, bold=True, max_width=740)
    else:
        col_w = (width-100) / len(stages)
        centers = []
        for col, stage in enumerate(stages):
            x = 50 + col * col_w
            text(x, 285, round_name(len(stage['matches'])).upper(), 27, accent, True, col_w-25, display=True)
            text(x, 322, f"GW{stage['gw']}", 20, accent, True)
            current = [top + (i+.5)*(bottom-top)/len(stage['matches']) for i in range(len(stage['matches']))]
            if col:
                for i, y in enumerate(current):
                    for previous in centers[2*i:2*i+2]:
                        d.line((x-22, previous, x-12, previous, x-12, y, x, y), fill=line, width=2)
            for match, y in zip(stage['matches'], current):
                w = col_w-25
                text(x+3, y-73, f"Матч {match['label']}", 16, QUIET)
                d.rounded_rectangle((x, y-51, x+w, y+51), radius=2, fill=CARD)
                d.line((x+16, y, x+w-16, y), fill=line, width=1)
                ids = participants(state, match)
                names = [slot_name(state, s) for s in match['slots']]
                for j, name in enumerate(names):
                    winner = ids[j] is not None and match.get('winner') == ids[j]
                    cy = y-25+j*50
                    color = muted if ids[j] is None or ('winner' in match and not winner) else ink
                    if winner:
                        d.rectangle((x, cy-25, x+4, cy+25), fill=accent)
                    score_width = max(38, d.textlength(str(match['scores'][j]), font=cup_font(24, winner))) if 'scores' in match else 0
                    text(x+16, cy, name, 24, color, winner,
                         w-32-(score_width+16 if score_width else 0), anchor='lm')
                    if 'scores' in match:
                        text(x+w-16, cy, match['scores'][j], 24, accent if winner else muted,
                             winner, score_width, anchor='rm')
                if 'scores' in match and match['scores'][0] == match['scores'][1]:
                    text(x+8, y+53, 'Ничья: победитель по жребию', 16, muted, max_width=w)
                if len(stage['matches']) == 1 and match.get('winner') is not None:
                    text(x+8, y+96, 'ЧЕМПИОН', 28, accent, True, w-16, display=True)
                    text(x+8, y+136, state['names'][str(match['winner'])], 26,
                         accent, True, w-16)
            centers = current
    d.line((50, height-76, width-50, height-76), fill=line, width=1)
    text(50, height-49, 'Очки за тур − штрафы за трансферы · жирным выделен победитель · источник: FPL', 20, muted)
    output = io.BytesIO()
    image.save(output, 'PNG', optimize=True)
    output.seek(0)
    output.name = 'cup-preliminary.png' if preliminary else 'cup-bracket.png'
    return output


class Cup:
    def __init__(self, db, fpl, league_id, chat_id):
        self.db, self.fpl, self.league_id, self.chat_id = db, fpl, league_id, chat_id

    async def latest(self):
        saved = await rows(self.db, 'SELECT * FROM cups ORDER BY id DESC LIMIT 1')
        return (saved[0]['id'], json.loads(saved[0]['state'])) if saved else (None, None)

    async def save(self, cup_id, state, stage):
        serialized = json.dumps(state, ensure_ascii=False)
        try:
            await self.db.execute('BEGIN')
            if cup_id is None:
                cursor = await self.db.execute('INSERT INTO cups(state) VALUES (?)', (serialized,))
                cup_id = cursor.lastrowid
            else:
                await self.db.execute('UPDATE cups SET state=? WHERE id=?', (serialized, cup_id))
            await self.db.execute('INSERT INTO cup_posts(cup_id,stage,state) VALUES (?,?,?)', (cup_id, stage, serialized))
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise
        return cup_id

    async def start(self, bootstrap, start=None):
        _, existing = await self.latest()
        if existing and not existing['stages'][-1].get('done'):
            raise ValueError('Кубок уже создан. /cup покажет сохранённую сетку.')
        now = datetime.now(timezone.utc)
        future = sorted(e['id'] for e in bootstrap['events']
                        if datetime.fromisoformat(e['deadline_time'].replace('Z', '+00:00')) > now)
        start = start if start is not None else (future[0] if future else None)
        if start not in future:
            raise ValueError('Выберите тур, дедлайн которого ещё не прошёл.')
        members = await self.fpl.members(self.league_id)
        state = draw_cup(members, start)
        if any(s['gw'] not in {e['id'] for e in bootstrap['events']} for s in state['stages']):
            raise ValueError('До конца сезона недостаточно туров для этого кубка.')
        await self.save(None, state, -1)
        return state

    async def advance(self, bootstrap):
        cup_id, state = await self.latest()
        if not state:
            return
        for index, stage in enumerate(state['stages']):
            if stage.get('done'):
                continue
            event = next((e for e in bootstrap['events'] if e['id'] == stage['gw']), {})
            if not (event.get('finished') and event.get('data_checked')):
                break
            scores = {}
            for match in stage['matches']:
                for entry in participants(state, match):
                    if entry is None:
                        raise ValueError('Missing previous cup winner')
                    history = await self.fpl.get(f'entry/{entry}/history/')
                    result = next((h for h in history['current'] if h['event'] == stage['gw']), None)
                    if result is None or any(type(result.get(k, 0)) is not int for k in ('points', 'event_transfers_cost')):
                        raise ValueError('Missing final cup score')
                    scores[entry] = result['points'] - result.get('event_transfers_cost', 0)
            state = settle(state, index, scores)
            await self.save(cup_id, state, index)

    async def show(self, bot, state, include_preliminary=True):
        if include_preliminary and state['stages'][0]['preliminary']:
            await bot.send_photo(chat_id=self.chat_id, photo=await asyncio.to_thread(render_cup, state, True),
                                 caption='Предварительный раунд · пары и путь в основную сетку')
        await bot.send_photo(chat_id=self.chat_id, photo=await asyncio.to_thread(render_cup, state),
                             caption='Кубок мини-лиги · основная сетка')

    async def deliver(self, bot):
        pending = await rows(self.db, 'SELECT * FROM cup_posts ORDER BY cup_id,stage')
        for post in pending:
            state, stage = json.loads(post['state']), post['stage']
            messages = text_chunks(announcement(state) if stage == -1 else summary(state, stage))
            preliminary = state['stages'][0]['preliminary'] and stage in (-1, 0)
            items = [('text', message) for message in messages]
            if preliminary:
                items.append(('photo', True))
            items.append(('photo', False))
            if post['uncertain']:
                # Do not repeat a potentially accepted Telegram send.
                logging.getLogger(__name__).error(
                    'Cup %s stage %s delivery uncertain; inspect chat, cup-status and cup-resolve',
                    post['cup_id'], stage)
                return
            for index in range(post['sent_count'], len(items)):
                kind, value = items[index]
                photo = await asyncio.to_thread(render_cup, state, value) if kind == 'photo' else None
                await self.db.execute('UPDATE cup_posts SET uncertain=1 WHERE cup_id=? AND stage=?', (post['cup_id'], stage))
                await self.db.commit()
                try:
                    if kind == 'text':
                        await bot.send_message(chat_id=self.chat_id, text=value)
                    else:
                        await bot.send_photo(chat_id=self.chat_id, photo=photo,
                                             caption='Кубок мини-лиги · ' + ('предварительный раунд' if value else 'основная сетка'))
                except (BadRequest, Forbidden, RetryAfter):
                    await self.db.execute('UPDATE cup_posts SET uncertain=0 WHERE cup_id=? AND stage=?', (post['cup_id'], stage))
                    await self.db.commit()
                    raise
                await self.db.execute('UPDATE cup_posts SET sent_count=?,uncertain=0 WHERE cup_id=? AND stage=?',
                                      (index+1, post['cup_id'], stage))
                await self.db.commit()
