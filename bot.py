import asyncio
import os
from dataclasses import dataclass
from typing import Optional

import psycopg
from psycopg.rows import tuple_row
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

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

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)

    if not predictions_open() and not prediction_complete(message.from_user.id):
        await message.answer(
            closed_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎯 Мой счёт", callback_data="user:score"),
                InlineKeyboardButton(text="🏆 Общий счёт", callback_data="user:leaderboard")
            ]])
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 ЗАПОЛНИТЬ ПРОГНОЗ", callback_data="user:start")],
        [
            InlineKeyboardButton(text="🎯 Мой счёт", callback_data="user:score"),
            InlineKeyboardButton(text="🏆 Общий счёт", callback_data="user:leaderboard"),
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
