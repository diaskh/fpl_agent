"""Deterministic deadline reports from public, locked FPL data."""
from datetime import datetime, timezone

CHIPS = {"wildcard": "Вайлдкард", "freehit": "Фри-хит", "bboost": "Бенч-буст", "3xc": "Тройной капитан"}


def passed_deadlines(bootstrap):
    now = datetime.now(timezone.utc)
    return sorted(e["id"] for e in bootstrap["events"]
                  if datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) <= now)


def free_transfers(history, chips, gw, settings):
    """Balance immediately after deadline, including next GW's allowance.

    Chip weeks preserve the bank without adding a transfer. Never infer a
    balance across missing history or silently assume special season top-ups.
    """
    by_event = {h["event"]: h for h in history}
    used_chips = {c["event"]: c["name"] for c in chips}
    if not by_event or gw not in by_event:
        return None
    start = min(by_event)
    bank = 1
    cap = 1 + settings.get("max_extra_free_transfers", 4)
    if type(cap) is not int or cap < 1:
        cap = 5
    for event in range(start + 1, gw + 1):
        row = by_event.get(event)
        if row is None or type(row.get("event_transfers")) is not int:
            return None
        if used_chips.get(event) not in ("wildcard", "freehit"):
            bank = min(cap, max(0, bank - row["event_transfers"]) + 1)
    return bank


async def build_messages(fpl, league_id, gw, bootstrap):
    names = {p["id"]: p["web_name"] for p in bootstrap["elements"]}
    blocks = []
    for member in await fpl.members(league_id):
        entry = member["entry"]
        picks = await fpl.get(f"entry/{entry}/event/{gw}/picks/")
        h = picks["entry_history"]
        if h["event"] != gw or len(picks["picks"]) != 15:
            raise ValueError("Locked squad not ready")
        history = await fpl.get(f"entry/{entry}/history/")
        transfers = await fpl.get(f"entry/{entry}/transfers/")
        transfers = [t for t in transfers if t["event"] == gw]
        chip = picks.get("active_chip")
        if chip == "freehit":
            # FPL transfer history omits Free Hit moves; show locked squad changes.
            previous_gw = gw - 1
            while previous_gw > 0:
                previous = await fpl.get(f"entry/{entry}/event/{previous_gw}/picks/")
                if previous.get("active_chip") != "freehit":
                    break
                previous_gw -= 1
            if previous_gw < 1 or len(previous["picks"]) != 15:
                raise ValueError("Previous permanent squad unavailable")
            old = {p["element"] for p in previous["picks"]}
            new = {p["element"] for p in picks["picks"]}
            transfers = []
        elif chip != "wildcard" and gw > min(r["event"] for r in history["current"]):
            if len(transfers) != h["event_transfers"]:
                raise ValueError("Transfer history not ready")
        chip = picks.get("active_chip")
        chips = [c for c in history.get("chips", []) if c["event"] != gw]
        if chip:
            chips.append({"event": gw, "name": chip})
        current = [r for r in history["current"] if r["event"] != gw] + [h]
        bank = free_transfers(current, chips, gw, bootstrap.get("game_settings", {}))
        lines = [f'{member["player_name"]} · {member["entry_name"]}']
        for t in sorted(transfers, key=lambda t: t.get("time", "")):
            lines.append(f'{names.get(t["element_out"], "#" + str(t["element_out"]))} → {names.get(t["element_in"], "#" + str(t["element_in"]))}')
        if chip == "freehit":
            lines.append("Фри-хит · изменения состава:")
            lines.append("Ушли: " + (", ".join(names.get(p, f"#{p}") for p in sorted(old - new)) or "нет"))
            lines.append("Пришли: " + (", ".join(names.get(p, f"#{p}") for p in sorted(new - old)) or "нет"))
        elif not transfers:
            lines.append("Без трансферов" if gw > 1 else "Стартовый состав")
        if chip:
            lines.append(f'Чип: {CHIPS.get(chip, chip)}')
        lines.append(f'Штраф: −{h["event_transfers_cost"]} очк.')
        lines.append(f'Бесплатных трансферов на GW{gw + 1}: {bank if bank is not None else "нет данных"}')
        blocks.append("\n".join(lines))
    if not blocks:
        raise ValueError("No managers")
    title = f"Трансферы и чипы · GW{gw}\nБаланс на момент дедлайна, с начислением следующего тура.\n\n"
    messages, message = [], title
    for block in blocks:
        for line in (block + "\n\n").splitlines(keepends=True):
            # Conservative limit also covers Telegram's UTF-16 length counting.
            if len((message + line).encode("utf-16-le")) // 2 > 3900:
                messages.append(message.rstrip())
                message = title
            message += line
    if message.strip():
        messages.append(message.rstrip())
    return messages
