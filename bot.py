import asyncio
import os
from io import BytesIO
from dataclasses import dataclass
from typing import Optional

import psycopg
from psycopg.rows import tuple_row
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

RICKROLL_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@dataclass(frozen=True)
class MatchDef:
    id: str
    title: str
    slot1: str
    slot2: str
    bo: int = 3


MATCHES = {
    "UB1": MatchDef("UB1", "Upper Bracket R1 · Match 1", "TEAM:Iron Wing", "TEAM:Team Spirit"),
    "UB2": MatchDef("UB2", "Upper Bracket R1 · Match 2", "TEAM:Team Vision", "TEAM:BoomBoys"),
    "UB3": MatchDef("UB3", "Upper Bracket R1 · Match 3", "TEAM:Team Liquid", "TEAM:Team Yandex"),
    "UB4": MatchDef("UB4", "Upper Bracket R1 · Match 4", "TEAM:Nigma Galaxy", "TEAM:Team Falcons"),

    "UB5": MatchDef("UB5", "Upper Bracket Semifinal 1", "WIN:UB1", "WIN:UB2"),
    "UB6": MatchDef("UB6", "Upper Bracket Semifinal 2", "WIN:UB3", "WIN:UB4"),

    "LB1": MatchDef("LB1", "Lower Bracket R1 · Match 1", "LOSE:UB1", "LOSE:UB2"),
    "LB2": MatchDef("LB2", "Lower Bracket R1 · Match 2", "LOSE:UB3", "LOSE:UB4"),

    "LB3": MatchDef("LB3", "Lower Bracket R2 · Match 1", "WIN:LB1", "LOSE:UB6"),
    "LB4": MatchDef("LB4", "Lower Bracket R2 · Match 2", "WIN:LB2", "LOSE:UB5"),

    "UB7": MatchDef("UB7", "Upper Bracket Final", "WIN:UB5", "WIN:UB6"),
    "LB5": MatchDef("LB5", "Lower Bracket R3", "WIN:LB3", "WIN:LB4"),
    "LB6": MatchDef("LB6", "Lower Bracket Final", "WIN:LB5", "LOSE:UB7"),

    "GF": MatchDef("GF", "🏆 GRAND FINAL", "WIN:UB7", "WIN:LB6", bo=5),
}

ORDER = [
    "UB1", "UB2", "UB3", "UB4",
    "UB5", "UB6",
    "LB1", "LB2",
    "LB3", "LB4",
    "UB7", "LB5", "LB6", "GF"
]


def conn():
    return psycopg.connect(DATABASE_URL, row_factory=tuple_row)


def init_db():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS picks (
                    user_id BIGINT NOT NULL,
                    match_id TEXT NOT NULL,
                    winner TEXT NOT NULL,
                    loser TEXT NOT NULL,
                    PRIMARY KEY (user_id, match_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS actual_results (
                    match_id TEXT PRIMARY KEY,
                    winner TEXT NOT NULL,
                    loser TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO settings(key, value)
                VALUES ('predictions_open', 'true')
                ON CONFLICT(key) DO NOTHING
            """)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def predictions_open() -> bool:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT value FROM settings WHERE key='predictions_open'"
            )
            row = cur.fetchone()
            return bool(row and row[0].lower() == "true")


def set_predictions_open(value: bool):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO settings(key, value)
                VALUES ('predictions_open', %s)
                ON CONFLICT(key)
                DO UPDATE SET value=EXCLUDED.value
            """, ("true" if value else "false",))


def register_user(user):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO users(user_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=EXCLUDED.username,
                    first_name=EXCLUDED.first_name
            """, (user.id, user.username, user.first_name))


def get_pick(user_id: int, match_id: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT winner, loser FROM picks WHERE user_id=%s AND match_id=%s",
                (user_id, match_id)
            )
            return cur.fetchone()


def save_pick(user_id: int, match_id: str, winner: str, loser: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO picks(user_id, match_id, winner, loser)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(user_id, match_id)
                DO UPDATE SET winner=EXCLUDED.winner, loser=EXCLUDED.loser
            """, (user_id, match_id, winner, loser))


def get_actual(match_id: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT winner, loser FROM actual_results WHERE match_id=%s",
                (match_id,)
            )
            return cur.fetchone()


def save_actual(match_id: str, winner: str, loser: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO actual_results(match_id, winner, loser)
                VALUES (%s, %s, %s)
                ON CONFLICT(match_id)
                DO UPDATE SET winner=EXCLUDED.winner, loser=EXCLUDED.loser
            """, (match_id, winner, loser))


def delete_actual_from(match_id: str):
    start = ORDER.index(match_id)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM actual_results WHERE match_id = ANY(%s)",
                (ORDER[start:],)
            )


def resolve_user_slot(user_id: int, slot: str) -> Optional[str]:
    kind, value = slot.split(":", 1)
    if kind == "TEAM":
        return value
    result = get_pick(user_id, value)
    if not result:
        return None
    return result[0] if kind == "WIN" else result[1]


def resolve_actual_slot(slot: str) -> Optional[str]:
    kind, value = slot.split(":", 1)
    if kind == "TEAM":
        return value
    result = get_actual(value)
    if not result:
        return None
    return result[0] if kind == "WIN" else result[1]


def resolved_user_match(user_id: int, match_id: str):
    m = MATCHES[match_id]
    return resolve_user_slot(user_id, m.slot1), resolve_user_slot(user_id, m.slot2)


def resolved_actual_match(match_id: str):
    m = MATCHES[match_id]
    return resolve_actual_slot(m.slot1), resolve_actual_slot(m.slot2)


def next_user_match(user_id: int) -> Optional[str]:
    for mid in ORDER:
        if get_pick(user_id, mid):
            continue
        a, b = resolved_user_match(user_id, mid)
        if a and b:
            return mid
    return None


def next_actual_match() -> Optional[str]:
    for mid in ORDER:
        if get_actual(mid):
            continue
        a, b = resolved_actual_match(mid)
        if a and b:
            return mid
    return None


def prediction_complete(user_id: int) -> bool:
    return all(get_pick(user_id, mid) for mid in ORDER)


def score(user_id: int):
    completed = 0
    correct = 0
    rows = []
    for mid in ORDER:
        actual = get_actual(mid)
        if not actual:
            continue
        completed += 1
        pred = get_pick(user_id, mid)
        ok = bool(pred and pred[0] == actual[0])
        correct += int(ok)
        rows.append((mid, pred[0] if pred else None, actual[0], ok))
    return correct, completed, rows


def score_text(user_id: int) -> str:
    correct, completed, rows = score(user_id)
    out = [
        "🎯 <b>ТВОЙ СЧЁТ</b>",
        f"<b>{correct}/{completed}</b> угадано среди сыгранных матчей",
        f"Реальных результатов внесено: {completed}/{len(ORDER)}",
    ]

    if completed:
        out.append(f"Точность: <b>{round(correct / completed * 100)}%</b>")

    if rows:
        out.append("\n<b>Последние результаты:</b>")
        for mid, pred, actual, ok in rows[-6:]:
            icon = "✅" if ok else "❌"
            p = esc(pred) if pred else "нет прогноза"
            out.append(f"{icon} <b>{mid}</b>: твой — {p}; факт — <b>{esc(actual)}</b>")

    if prediction_complete(user_id):
        champ = get_pick(user_id, "GF")[0]
        out.append(f"\n🏆 Твой прогноз на чемпиона: <b>{esc(champ)}</b>")

    return "\n".join(out)


def leaderboard_text(limit: int = 20) -> str:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT user_id, username, first_name FROM users")
            users = cur.fetchall()

    board = []
    for uid, username, first_name in users:
        if not prediction_complete(uid):
            continue
        correct, completed, _ = score(uid)
        name = f"@{username}" if username else (first_name or str(uid))
        board.append((correct, completed, name))

    board.sort(key=lambda x: (-x[0], x[2].lower()))
    out = ["🏅 <b>ТАБЛИЦА ПРОГНОЗОВ</b>"]

    if not board:
        out.append("Пока никто не закончил прогноз.")
        return "\n".join(out)

    for i, (correct, completed, name) in enumerate(board[:limit], 1):
        out.append(f"{i}. {esc(name)} — <b>{correct}/{completed}</b>")
    return "\n".join(out)


def closed_text() -> str:
    return (
        "🔒 <b>Приём прогнозов закрыт.</b>\n\n"
        "Новые прогнозы и продолжение незавершённых прогнозов сейчас недоступны.\n"
        "Уже сохранённые результаты остаются в системе."
    )


def user_keyboard(mid: str, a: str, b: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"1️⃣ {a}", callback_data=f"pick:{mid}:1"),
            InlineKeyboardButton(text=f"2️⃣ {b}", callback_data=f"pick:{mid}:2"),
        ],
        [InlineKeyboardButton(text="🎯 Мой счёт", callback_data="user:score")]
    ])


def admin_keyboard(mid: str, a: str, b: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ {a}", callback_data=f"actual:{mid}:1"),
        InlineKeyboardButton(text=f"✅ {b}", callback_data=f"actual:{mid}:2"),
    ]])


async def send_next_user(message: Message, user_id: int):
    if not predictions_open():
        await message.answer(closed_text(), parse_mode="HTML")
        return

    mid = next_user_match(user_id)
    if not mid:
        if prediction_complete(user_id):
            await message.answer(
                "✅ <b>Прогноз полностью заполнен!</b>\n\n" + score_text(user_id),
                parse_mode="HTML"
            )
        return

    m = MATCHES[mid]
    a, b = resolved_user_match(user_id, mid)
    await message.answer(
        f"<b>{m.title}</b> · BO{m.bo}\n\nКто победит?\n\n"
        f"<b>{esc(a)}</b> ⚔️ <b>{esc(b)}</b>",
        parse_mode="HTML",
        reply_markup=user_keyboard(mid, a, b)
    )


async def send_admin_next(message: Message):
    mid = next_actual_match()
    if not mid:
        await message.answer("🏆 Все реальные результаты внесены.")
        return

    m = MATCHES[mid]
    a, b = resolved_actual_match(mid)
    await message.answer(
        f"🛠 <b>ВНЕСТИ РЕАЛЬНЫЙ РЕЗУЛЬТАТ</b>\n\n"
        f"<b>{mid}</b> · {m.title}\n"
        f"{esc(a)} ⚔️ {esc(b)}\n\nКто реально победил?",
        parse_mode="HTML",
        reply_markup=admin_keyboard(mid, a, b)
    )


async def notify_all(bot: Bot, mid: str, winner: str):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()

    for (uid,) in users:
        try:
            correct, completed, _ = score(uid)
            await bot.send_message(
                uid,
                f"📣 Результат <b>{mid}</b>: <b>{esc(winner)}</b> победил.\n\n"
                f"Твой текущий счёт: <b>{correct}/{completed}</b>\n"
                f"/score — подробнее",
                parse_mode="HTML"
            )
        except Exception:
            pass



def admin_matches_text() -> str:
    out = ["🛠 <b>РЕАЛЬНЫЕ РЕЗУЛЬТАТЫ</b>"]

    for title, mids in [
        ("🔼 Верхняя сетка", ["UB1","UB2","UB3","UB4","UB5","UB6","UB7"]),
        ("🔽 Нижняя сетка", ["LB1","LB2","LB3","LB4","LB5","LB6"]),
        ("🏆 Финал", ["GF"]),
    ]:
        out.append(f"\n<b>{title}</b>")
        for mid in mids:
            actual = get_actual(mid)
            a, b = resolved_actual_match(mid)
            if actual:
                out.append(f"✅ <b>{mid}</b> — {esc(actual[0])}")
            elif a and b:
                out.append(f"🎯 <b>{mid}</b> — {esc(a)} vs {esc(b)}")
            else:
                out.append(f"🔒 <b>{mid}</b> — ждёт предыдущих матчей")
    return "\n".join(out)


def admin_matches_keyboard():
    rows = []
    for mid in ORDER:
        if get_actual(mid):
            continue
        a, b = resolved_actual_match(mid)
        if a and b:
            rows.append([
                InlineKeyboardButton(
                    text=f"🎯 {mid}: {a} vs {b}",
                    callback_data=f"adminmatch:{mid}"
                )
            ])

    rows.append([
        InlineKeyboardButton(text="🏆 Общий счёт", callback_data="admin:leaderboard"),
        InlineKeyboardButton(text="📋 Обновить", callback_data="admin:matches")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_winners(bot: Bot):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT user_id, username, first_name FROM users")
            users = cur.fetchall()

    results = []
    for uid, username, first_name in users:
        if not prediction_complete(uid):
            continue
        correct, completed, _ = score(uid)
        results.append((uid, correct, completed))

    if not results:
        return

    best_score = max(correct for _, correct, _ in results)
    winners = [(uid, completed) for uid, correct, completed in results if correct == best_score]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 ЗАБРАТЬ АРКАНУ", url=RICKROLL_URL)
    ]])

    for uid, completed in winners:
        try:
            await bot.send_message(
                uid,
                "🎉 <b>ПОЗДРАВЛЯЕМ!</b>\n\n"
                "Вы самый лучший прогнозёр The International!\n"
                f"Ваш результат: <b>{best_score}/{completed}</b>\n\n"
                "🎁 Вы выиграли <b>Аркану</b>!\n"
                "Нажмите кнопку ниже, чтобы получить приз:",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception:
            pass


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _prediction_status(user_id: int, mid: str):
    pred = get_pick(user_id, mid)
    actual = get_actual(mid)

    if not pred:
        return "empty", None, None

    predicted_winner = pred[0]

    if not actual:
        return "pending", predicted_winner, None

    actual_winner = actual[0]
    if predicted_winner == actual_winner:
        if mid == "GF":
            return "champion", predicted_winner, actual_winner
        return "correct", predicted_winner, actual_winner

    return "wrong", predicted_winner, actual_winner


def render_bracket_png(user_id: int, display_name: str) -> bytes:
    W, H = 1800, 1180
    BG = (19, 22, 28)
    PANEL = (34, 39, 48)
    BORDER = (72, 80, 94)
    TEXT = (238, 242, 247)
    MUTED = (160, 170, 184)
    GREEN = (44, 180, 105)
    RED = (225, 75, 75)
    GRAY = (96, 105, 120)
    GOLD = (230, 181, 55)

    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)

    title_font = _font(44, True)
    subtitle_font = _font(27, False)
    section_font = _font(28, True)
    match_font = _font(22, True)
    small_font = _font(18, False)
    score_font = _font(25, True)

    correct, completed, _ = score(user_id)
    total = len(ORDER)
    accuracy = round(correct / completed * 100) if completed else 0

    draw.text((60, 42), "THE INTERNATIONAL 2026 - MY PREDICTION", font=title_font, fill=TEXT)
    draw.text((60, 100), display_name, font=subtitle_font, fill=MUTED)
    draw.text(
        (60, 145),
        f"Score: {correct}/{completed} played   |   Accuracy: {accuracy}%   |   Total matches: {total}",
        font=score_font,
        fill=TEXT
    )

    # Legend
    legend = [
        ("CORRECT", GREEN),
        ("WRONG", RED),
        ("NOT PLAYED", GRAY),
        ("CHAMPION CORRECT", GOLD),
    ]
    lx = 60
    for label, color in legend:
        draw.rounded_rectangle((lx, 202, lx + 28, 230), radius=6, fill=color)
        draw.text((lx + 38, 202), label, font=small_font, fill=MUTED)
        lx += 260 if label != "CHAMPION CORRECT" else 330

    box_w, box_h = 270, 86

    # Visual positions approximate a double-elimination bracket.
    positions = {
        "UB1": (60, 310), "UB2": (60, 420), "UB3": (60, 530), "UB4": (60, 640),
        "UB5": (390, 365), "UB6": (390, 585),
        "UB7": (720, 475),
        "GF":  (1430, 475),

        "LB1": (390, 800), "LB2": (390, 910),
        "LB3": (720, 800), "LB4": (720, 910),
        "LB5": (1035, 855),
        "LB6": (1240, 690),
    }

    # Connector topology for display.
    edges = [
        ("UB1","UB5"), ("UB2","UB5"), ("UB3","UB6"), ("UB4","UB6"),
        ("UB5","UB7"), ("UB6","UB7"), ("UB7","GF"),
        ("LB1","LB3"), ("LB2","LB4"), ("LB3","LB5"), ("LB4","LB5"),
        ("LB5","LB6"), ("LB6","GF"),
    ]

    # Draw connections behind boxes.
    for a_mid, b_mid in edges:
        if a_mid not in positions or b_mid not in positions:
            continue
        ax, ay = positions[a_mid]
        bx, by = positions[b_mid]
        x1, y1 = ax + box_w, ay + box_h // 2
        x2, y2 = bx, by + box_h // 2
        midx = (x1 + x2) // 2
        draw.line((x1, y1, midx, y1), fill=BORDER, width=4)
        draw.line((midx, y1, midx, y2), fill=BORDER, width=4)
        draw.line((midx, y2, x2, y2), fill=BORDER, width=4)

    draw.text((60, 260), "UPPER BRACKET", font=section_font, fill=TEXT)
    draw.text((60, 755), "LOWER BRACKET", font=section_font, fill=TEXT)

    def truncate(s, n=22):
        return s if len(s) <= n else s[:n-1] + "…"

    for mid, (x, y) in positions.items():
        m = MATCHES[mid]
        a, b = resolved_user_match(user_id, mid)
        status, pred_winner, actual_winner = _prediction_status(user_id, mid)

        color = {
            "correct": GREEN,
            "wrong": RED,
            "pending": GRAY,
            "empty": BORDER,
            "champion": GOLD,
        }[status]

        draw.rounded_rectangle(
            (x, y, x + box_w, y + box_h),
            radius=14,
            fill=PANEL,
            outline=color,
            width=5
        )

        draw.text((x + 14, y + 10), mid, font=match_font, fill=color)

        if a and b:
            draw.text((x + 68, y + 11), f"{truncate(a, 16)} vs {truncate(b, 16)}",
                      font=small_font, fill=TEXT)
        else:
            draw.text((x + 68, y + 11), "waiting for previous matches",
                      font=small_font, fill=MUTED)

        if pred_winner:
            draw.text((x + 14, y + 46), f"Pick: {truncate(pred_winner)}",
                      font=small_font, fill=TEXT)

        if actual_winner:
            result_label = "Correct" if status in ("correct", "champion") else f"Actual: {truncate(actual_winner)}"
            draw.text((x + 145, y + 46), result_label,
                      font=small_font, fill=color)
        elif pred_winner:
            draw.text((x + 175, y + 46), "Pending",
                      font=small_font, fill=MUTED)

    champ_pick = get_pick(user_id, "GF")
    if champ_pick:
        champ = champ_pick[0]
        actual_gf = get_actual("GF")
        champ_status = "pending"
        champ_color = GRAY
        if actual_gf:
            if champ == actual_gf[0]:
                champ_status = "CORRECT"
                champ_color = GOLD
            else:
                champ_status = "WRONG"
                champ_color = RED

        draw.rounded_rectangle((1320, 1020, 1740, 1120), radius=18, fill=PANEL, outline=champ_color, width=5)
        draw.text((1345, 1038), "PREDICTED CHAMPION", font=small_font, fill=MUTED)
        draw.text((1345, 1068), truncate(champ, 25), font=section_font, fill=champ_color)
        draw.text((1620, 1040), champ_status, font=small_font, fill=champ_color)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def send_bracket_image(message: Message, user):
    if not prediction_complete(user.id):
        await message.answer(
            "Сетка появится после того, как ты полностью закончишь прогноз."
        )
        return

    name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    png = render_bracket_png(user.id, name)
    photo = BufferedInputFile(png, filename=f"ti2026_bracket_{user.id}.png")
    await message.answer_photo(
        photo,
        caption="🗺 <b>Твоя сетка TI 2026</b>\n"
                "Зелёный — угадано, красный — ошибка, серый — матч ещё не сыгран, "
                "золотой — правильно угаданный чемпион.",
        parse_mode="HTML"
    )

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)

    if not predictions_open() and not prediction_complete(message.from_user.id):
        await message.answer(
            closed_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎯 Мой счёт", callback_data="user:score"),
                    InlineKeyboardButton(text="🏆 Общий счёт", callback_data="user:leaderboard")
                ],
                [
                    InlineKeyboardButton(text="🗺 Моя сетка", callback_data="user:bracket")
                ]
            ])
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ЗАПОЛНИТЬ ПРОГНОЗ", callback_data="user:start")],
        [
            InlineKeyboardButton(text="🎯 Мой счёт", callback_data="user:score"),
            InlineKeyboardButton(text="🏆 Общий счёт", callback_data="user:leaderboard"),
        ],
        [
            InlineKeyboardButton(text="🗺 Моя сетка", callback_data="user:bracket")
        ]
    ])
    await message.answer(
        "🏆 <b>TI 2026 Bracket Predictor</b>\n\n"
        "Заполни всю сетку один раз. После начала турнира админ будет "
        "вносить реальные результаты, а бот автоматически посчитает, "
        "сколько матчей ты угадал.",
        parse_mode="HTML",
        reply_markup=kb
    )



@dp.message(Command("bracket"))
async def bracket_cmd(message: Message):
    register_user(message.from_user)
    await send_bracket_image(message, message.from_user)


@dp.message(Command("score"))
async def score_cmd(message: Message):
    register_user(message.from_user)
    await message.answer(score_text(message.from_user.id), parse_mode="HTML")


@dp.message(Command("leaderboard"))
async def leaderboard_cmd(message: Message):
    await message.answer(leaderboard_text(), parse_mode="HTML")


@dp.message(Command("open"))
async def open_predictions(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    set_predictions_open(True)
    await message.answer("🔓 <b>Приём прогнозов открыт.</b>", parse_mode="HTML")


@dp.message(Command("close"))
async def close_predictions(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    set_predictions_open(False)
    await message.answer(
        "🔒 <b>Приём прогнозов закрыт.</b>\n\n"
        "Никто больше не сможет начать или продолжить заполнение прогноза.",
        parse_mode="HTML"
    )


@dp.message(Command("status"))
async def predictions_status(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if predictions_open():
        await message.answer("🟢 Прогнозы сейчас <b>ОТКРЫТЫ</b>.", parse_mode="HTML")
    else:
        await message.answer("🔴 Прогнозы сейчас <b>ЗАКРЫТЫ</b>.", parse_mode="HTML")


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            f"⛔ Нет доступа.\nТвой Telegram ID: <code>{message.from_user.id}</code>",
            parse_mode="HTML"
        )
        return

    await message.answer(
        admin_matches_text(),
        parse_mode="HTML",
        reply_markup=admin_matches_keyboard()
    )


@dp.message(Command("undoresult"))
async def undo_result(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or parts[1] not in MATCHES:
        await message.answer("Использование: /undoresult UB1")
        return

    delete_actual_from(parts[1])
    await message.answer(f"♻️ Результаты начиная с {parts[1]} удалены.")
    await send_admin_next(message)


@dp.callback_query(F.data == "user:start")
async def user_start(call: CallbackQuery):
    register_user(call.from_user)
    await call.answer()

    if prediction_complete(call.from_user.id):
        await call.message.answer(
            "Твой прогноз уже зафиксирован.\n\n" + score_text(call.from_user.id),
            parse_mode="HTML"
        )
        return

    if not predictions_open():
        await call.message.answer(closed_text(), parse_mode="HTML")
        return

    await send_next_user(call.message, call.from_user.id)


@dp.callback_query(F.data.startswith("pick:"))
async def pick(call: CallbackQuery):
    register_user(call.from_user)

    # Critical: even old inline buttons stop working after /close.
    if not predictions_open():
        await call.answer("Приём прогнозов закрыт.", show_alert=True)
        return

    _, mid, side = call.data.split(":")
    uid = call.from_user.id

    if get_pick(uid, mid):
        await call.answer("Этот матч уже выбран.")
        return

    a, b = resolved_user_match(uid, mid)
    if not a or not b:
        await call.answer("Матч ещё не сформирован.", show_alert=True)
        return

    winner, loser = (a, b) if side == "1" else (b, a)
    save_pick(uid, mid, winner, loser)

    await call.answer(f"Выбрано: {winner}")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_next_user(call.message, uid)


@dp.callback_query(F.data == "user:score")
async def user_score(call: CallbackQuery):
    register_user(call.from_user)
    await call.answer()
    await call.message.answer(score_text(call.from_user.id), parse_mode="HTML")



@dp.callback_query(F.data == "user:bracket")
async def user_bracket(call: CallbackQuery):
    register_user(call.from_user)
    await call.answer()
    await send_bracket_image(call.message, call.from_user)


@dp.callback_query(F.data == "user:leaderboard")
async def user_leaderboard(call: CallbackQuery):
    await call.answer()
    await call.message.answer(leaderboard_text(), parse_mode="HTML")



@dp.callback_query(F.data == "admin:matches")
async def admin_matches(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        admin_matches_text(),
        parse_mode="HTML",
        reply_markup=admin_matches_keyboard()
    )


@dp.callback_query(F.data == "admin:leaderboard")
async def admin_leaderboard(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return
    await call.answer()
    await call.message.answer(leaderboard_text(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adminmatch:"))
async def admin_choose_match(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, mid = call.data.split(":", 1)

    if get_actual(mid):
        await call.answer("Этот результат уже внесён.")
        return

    a, b = resolved_actual_match(mid)
    if not a or not b:
        await call.answer("Этот матч ещё не сформирован.", show_alert=True)
        return

    m = MATCHES[mid]
    await call.answer()
    await call.message.answer(
        f"🛠 <b>{mid}</b> · {m.title}\n\n"
        f"{esc(a)} ⚔️ {esc(b)}\n\n"
        "Кто реально победил?",
        parse_mode="HTML",
        reply_markup=admin_keyboard(mid, a, b)
    )


@dp.callback_query(F.data.startswith("actual:"))
async def actual_result(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, mid, side = call.data.split(":")
    if get_actual(mid):
        await call.answer("Результат уже внесён.")
        return

    a, b = resolved_actual_match(mid)
    if not a or not b:
        await call.answer("Реальная пара ещё не сформирована.", show_alert=True)
        return

    winner, loser = (a, b) if side == "1" else (b, a)
    save_actual(mid, winner, loser)

    await call.answer(f"Результат: {winner}")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.message.answer(
        f"✅ Реальный результат <b>{mid}</b>: <b>{esc(winner)}</b> победил.",
        parse_mode="HTML"
    )
    await notify_all(bot, mid, winner)

    if mid == "GF":
        await notify_winners(bot)

    await call.message.answer(
        admin_matches_text(),
        parse_mode="HTML",
        reply_markup=admin_matches_keyboard()
    )


async def main():
    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
