# main.py
"""
Mukammal boshlang'ich Telegram "reward & tasks" bot
Funktsiyalar (1-bosqich va ko'p yuqori prioritet elementlar):
- users, transactions, referrals
- daily bonus (0.2-5.0 rubl) + streak
- send money (ID orqali, confirm)
- admin order channel (orders -> ADMIN_CHANNEL)
- daily quiz (with scoring & reward)
- spin wheel (daily free spin)
- missions/tasks + shop
- leaderboard & transactions history
- basic anti-fraud (rate limits, duplicate referals prevention)
- scheduler (daily audits)
- placeholders for YouTube/Instagram OAuth checks
"""

import os
import sqlite3
import logging
import random
import datetime
import math
import json
from functools import wraps
from typing import Optional

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_CHANNEL = os.getenv("ADMIN_CHANNEL", "@your_admin_channel")  # channel where orders & logs go
BASE_URL = os.getenv("BASE_URL", "")  # for OAuth callbacks if used
# GOOGLE_CLIENT_ID / SECRET for YouTube features (placeholders)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# Anti-fraud & limits
DAILY_MAX_BONUS_PER_USER = 20.0  # safety cap on how much bonus a user can get per day
MAX_REFERRALS_PER_USER = 1000  # arbitrary safety clamp
MAX_SEND_PER_DAY = 500.0  # cap on money a user can send per day
MIN_SPIN_COST = 0.0  # if paid spins implemented

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------- DATABASE -----------------
DB_PATH = "bot_full.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

# Create necessary tables
cur.executescript("""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    balance REAL DEFAULT 0,
    last_bonus DATE,
    streak INTEGER DEFAULT 0,
    referred_by INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_banned INTEGER DEFAULT 0,
    daily_sent REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT, -- 'bonus','transfer_in','transfer_out','penalty','reward','quiz','spin'
    amount REAL,
    reason TEXT,
    meta TEXT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    status TEXT DEFAULT 'new',
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer INTEGER,
    referred INTEGER,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channels (
    username TEXT PRIMARY KEY,
    type TEXT -- 'tg','yt','insta'
);

CREATE TABLE IF NOT EXISTS user_subscriptions (
    user_id INTEGER,
    channel_username TEXT,
    subscribed INTEGER,
    last_checked DATETIME,
    PRIMARY KEY (user_id, channel_username)
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    q TEXT,
    options TEXT, -- json list
    answer_index INTEGER,
    reward REAL DEFAULT 0.2
);

CREATE TABLE IF NOT EXISTS user_quiz_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    question_id INTEGER,
    correct INTEGER,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    reward REAL,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    title TEXT,
    description TEXT,
    reward REAL,
    condition_json TEXT -- flexible conditions in JSON
);

CREATE TABLE IF NOT EXISTS user_missions (
    user_id INTEGER,
    mission_id INTEGER,
    progress_json TEXT,
    completed INTEGER DEFAULT 0,
    completed_at DATETIME
);
""")
conn.commit()

# --------------- UTILITIES -----------------
def db_commit():
    conn.commit()

def ensure_user(user_id: int, user_obj: Optional[Message]=None, referred_by: Optional[int]=None):
    """Create user row if not exists and update basic info"""
    cur.execute("SELECT id FROM users WHERE id=?", (user_id,))
    if cur.fetchone() is None:
        username = user_obj.from_user.username if user_obj else None
        first = user_obj.from_user.first_name if user_obj else None
        last = user_obj.from_user.last_name if user_obj else None
        cur.execute(
            "INSERT INTO users (id, username, first_name, last_name, referred_by) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first, last, referred_by)
        )
        db_commit()
    else:
        # update possible username/first_name changes
        if user_obj:
            cur.execute(
                "UPDATE users SET username=?, first_name=?, last_name=? WHERE id=?",
                (user_obj.from_user.username, user_obj.from_user.first_name, user_obj.from_user.last_name, user_id)
            )
            db_commit()

def get_balance(user_id: int) -> float:
    cur.execute("SELECT balance FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    return float(row[0]) if row else 0.0

def add_transaction(user_id: int, ttype: str, amount: float, reason: str="", meta: dict=None):
    """Add transaction and update balance atomically"""
    if meta is None:
        meta = {}
    # Ensure user exists
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    # Update balance
    cur.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
    meta_json = json.dumps(meta, ensure_ascii=False)
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount, reason, meta) VALUES (?, ?, ?, ?, ?)",
        (user_id, ttype, amount, reason, meta_json)
    )
    db_commit()

def safe_round(amount: float) -> float:
    # Round to 2 decimals carefully
    return round(float(math.floor(amount * 100 + 0.5)) / 100.0, 2)

def can_receive_bonus_today(user_id: int) -> bool:
    cur.execute("SELECT last_bonus FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return True
    last = row[0]
    today = datetime.date.today().isoformat()
    return last != today

def update_last_bonus_and_streak(user_id: int, bonus_amount: float):
    cur.execute("SELECT last_bonus, streak FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    last_bonus, streak = (row if row else (None, 0))
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    if last_bonus == yesterday:
        streak = (streak or 0) + 1
    else:
        streak = 1
    cur.execute("UPDATE users SET last_bonus=?, streak=? WHERE id=?", (today, streak, user_id))
    db_commit()
    return streak

def notify_admin(app, text: str):
    # send message to admin channel (non-blocking)
    try:
        app.bot.send_message(ADMIN_CHANNEL, text)
    except Exception as e:
        logger.warning("Notify admin failed: %s", e)

# --------------- ANTI-FRAUD HELPERS -----------------
def rate_limited(max_per_minute=10):
    # simplistic in-memory rate limiter per user
    calls = {}
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            uid = user.id if user else 0
            now = datetime.datetime.utcnow().timestamp()
            window = 60
            user_calls = calls.get(uid, [])
            # remove old
            user_calls = [t for t in user_calls if now - t < window]
            if len(user_calls) >= max_per_minute:
                await update.message.reply_text("⏱ Siz juda tez harakat qilyapsiz. Iltimos biroz kuting.")
                return
            user_calls.append(now)
            calls[uid] = user_calls
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# --------------- BOT FEATURES -----------------

# ----- START & REFERRAL -----
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    ref_id = None
    if args:
        a = args[0]
        if a.startswith("ref"):
            try:
                ref_id = int(a[3:])
            except:
                ref_id = None
    ensure_user(user.id, update.message, ref_id)
    # if referred
    if ref_id and ref_id != user.id:
        # prevent double awarding for same referred: check referrals table
        cur.execute("SELECT id FROM referrals WHERE referred=?",(user.id,))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO referrals (referrer, referred) VALUES (?, ?)", (ref_id, user.id))
            add_transaction(ref_id, "referral", 4.0, f"Referral for {user.id}", {"referred": user.id})
            db_commit()
            try:
                await context.bot.send_message(ref_id, f"🎉 Sizga +4 rubl referal bonusi! (ID: {user.id})")
            except Exception:
                pass

    # Menu
    kb = [
        [KeyboardButton("🎁 Kunlik bonus"), KeyboardButton("🎯 Daily Quiz")],
        [KeyboardButton("💠 Spin Wheel"), KeyboardButton("📤 Pul yuborish")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("📥 Tranzaksiyalar")],
        [KeyboardButton("🛒 Shop"), KeyboardButton("🔗 Referal havola")],
        [KeyboardButton("📝 Buyurtma berish")]
    ]
    markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
    ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref{user.id}"
    await update.message.reply_text(
        f"Assalomu alaykum, {user.first_name}!\n"
        f"Balans: {safe_round(get_balance(user.id))} rubl\n"
        f"Referal havolangiz:\n{ref_link}",
        reply_markup=markup
    )

# ----- BALANCE & TRANSACTIONS -----
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    bal = safe_round(get_balance(uid))
    await update.message.reply_text(f"💰 Balansingiz: {bal} rubl")

async def transactions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cur.execute("SELECT ts, type, amount, reason FROM transactions WHERE user_id=? ORDER BY ts DESC LIMIT 20", (uid,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("📭 Sizda tranzaksiyalar yo‘q.")
        return
    text = "📜 So'nggi tranzaksiyalar:\n"
    for ts, ttype, amt, reason in rows:
        text += f"{ts.split('.')[0]} — {amt:+} rubl — {ttype} — {reason}\n"
    await update.message.reply_text(text)

# ----- DAILY BONUS -----
@rate_limited(max_per_minute=6)
async def daily_bonus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    if not can_receive_bonus_today(uid):
        await update.message.reply_text("⚠️ Siz bugungi bonusni allaqachon olgansiz. Ertaga yana urinib ko‘ring.")
        return
    # base random bonus
    base = round(random.uniform(0.2, 5.0), 2)
    # update streak
    streak = update_last_bonus_and_streak(uid, base)  # returns new streak
    streak_bonus = round(streak * 0.2, 2)
    total = safe_round(base + streak_bonus)
    # safety clamp per day
    if total > DAILY_MAX_BONUS_PER_USER:
        total = DAILY_MAX_BONUS_PER_USER
    add_transaction(uid, "bonus", total, f"daily_bonus (base {base} + streak {streak_bonus})", {"streak": streak})
    await update.message.reply_text(
        f"🎁 Bugungi bonus: {base} rubl\n🔥 Ketma-ket: {streak} kun (+{streak_bonus} rubl)\n"
        f"✅ Jami qo‘shildi: {total} rubl\nBalans: {safe_round(get_balance(uid))} rubl"
    )
    # admin notify
    notify_admin(context.application, f"👤 {uid} bonus oldi: {total} rubl (streak {streak})")

# ----- SEND MONEY FLOW -----
SEND_RECIPIENT, SEND_AMOUNT, SEND_CONFIRM = range(3)

async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    await update.message.reply_text("📤 Pul yuborish: qabul qiluvchining ID sini kiriting (raqam):")
    return SEND_RECIPIENT

async def send_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❗ Iltimos faqat raqamli ID kiriting. Masalan: 123456789")
        return SEND_RECIPIENT
    rid = int(text)
    context.user_data['send_recipient'] = rid
    await update.message.reply_text("💰 Yuboriladigan summani kiriting (masalan: 1 yoki 0.5):")
    return SEND_AMOUNT

async def send_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        # digit-by-digit style: validate chars
        parts = text.split(".")
        if len(parts) > 2:
            raise ValueError
        int_part = parts[0] if parts[0] != "" else "0"
        frac_part = parts[1] if len(parts) == 2 else "0"
        if not int_part.lstrip("-").isdigit() or not frac_part.isdigit():
            raise ValueError
        if len(frac_part) > 2:
            frac_part = frac_part[:2]
        amount = float(int_part) + float("0." + frac_part) if not int_part.startswith("-") else - (float(int_part.lstrip("-")) + float("0." + frac_part))
    except Exception:
        await update.message.reply_text("❗ Noto'g'ri summa. Iltimos son kiriting (masalan: 1 yoki 0.5).")
        return SEND_AMOUNT

    amount = safe_round(amount)
    if amount <= 0:
        await update.message.reply_text("❗ Iltimos, musbat summa kiriting.")
        return SEND_AMOUNT

    sender = update.effective_user.id
    bal = get_balance(sender)
    if amount > bal:
        await update.message.reply_text(f"⚠️ Sizda yetarli mablag‘ yo‘q. Balans: {bal} rubl")
        return ConversationHandler.END

    # daily send limit check
    cur.execute("SELECT daily_sent FROM users WHERE id=?", (sender,))
    row = cur.fetchone()
    daily_sent = float(row[0]) if row and row[0] else 0.0
    if (daily_sent + amount) > MAX_SEND_PER_DAY:
        await update.message.reply_text("⚠️ Bugungi yuborish limitiga yetdingiz.")
        return ConversationHandler.END

    context.user_data['send_amount'] = amount
    rec = context.user_data['send_recipient']
    # confirm inline
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data="confirm_send"), InlineKeyboardButton("❌ Bekor", callback_data="cancel_send")]
    ])
    await update.message.reply_text(f"Yuborishni tasdiqlaysizmi?\nID: {rec}\nSumma: {amount} rubl", reply_markup=kb)
    return SEND_CONFIRM

async def send_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user.id
    data = q.data
    if data == "cancel_send":
        await q.edit_message_text("❌ Pul yuborish bekor qilindi.")
        return ConversationHandler.END
    rec = context.user_data.get('send_recipient')
    amt = context.user_data.get('send_amount')
    if rec is None or amt is None:
        await q.edit_message_text("❗ Ma'lumot topilmadi.")
        return ConversationHandler.END
    # ensure recipient exists
    ensure_user(rec)
    # re-check balance
    bal = get_balance(user)
    if amt > bal:
        await q.edit_message_text(f"⚠️ Sizda endi yetarli mablag' yo'q. Balans: {bal}")
        return ConversationHandler.END
    # perform atomic-ish transfer
    add_transaction(user, "transfer_out", -amt, f"to {rec}", {"to": rec})
    add_transaction(rec, "transfer_in", amt, f"from {user}", {"from": user})
    # update daily_sent
    cur.execute("UPDATE users SET daily_sent = daily_sent + ? WHERE id=?", (amt, user))
    db_commit()
    await q.edit_message_text(f"✅ Muvaffaqiyatli! {amt} rubl yuborildi (ID: {rec}).")
    try:
        await context.bot.send_message(rec, f"📥 Sizga {amt} rubl yuborildi! Jo'natuvchi ID: {user}\nBalansingiz: {safe_round(get_balance(rec))} rubl")
    except Exception:
        # recipient may not have started bot; ignore
        pass
    # admin notify
    notify_admin(context.application, f"💸 Transfer: {user} -> {rec} : {amt} rubl")
    return ConversationHandler.END

# ----- ORDER (Buyurtma) -----
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    await update.message.reply_text("📝 Buyurtma matnini yozing (mahsulot, aloqa, manzil):")
    context.user_data['awaiting_order'] = True

async def order_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_order'):
        uid = update.effective_user.id
        text = update.message.text
        cur.execute("INSERT INTO orders (user_id, text) VALUES (?, ?)", (uid, text))
        db_commit()
        # send to admin channel
        try:
            await context.bot.send_message(ADMIN_CHANNEL, f"🆕 Yangi buyurtma\nUser: {uid}\nText: {text}")
        except Exception:
            pass
        context.user_data['awaiting_order'] = False
        await update.message.reply_text("✅ Buyurtmangiz qabul qilindi. Tez orada adminlar bog'lanadi.")
    else:
        await update.message.reply_text("Noma'lum xabar. Menyudan biror tugmani tanlang.")

# ----- QUIZ (Daily Quiz) -----
# conversation flow: user presses Daily Quiz -> serve 1 question at random (not yet answered today)
QUIZ_ASK, QUIZ_ANSWER = range(2)

def get_random_quiz_question(user_id: int):
    # select question not answered yet today by user; fallback random
    today = datetime.date.today().isoformat()
    cur.execute("""
        SELECT q.id, q.q, q.options, q.answer_index, q.reward
        FROM quiz_questions q
        WHERE q.id NOT IN (
            SELECT question_id FROM user_quiz_history WHERE user_id=? AND DATE(ts)=?
        )
        ORDER BY RANDOM() LIMIT 1
    """, (user_id, today))
    row = cur.fetchone()
    if not row:
        # allow repeat if none left
        cur.execute("SELECT id, q, options, answer_index, reward FROM quiz_questions ORDER BY RANDOM() LIMIT 1")
        row = cur.fetchone()
    if not row:
        return None
    qid, qtext, opts_json, answer_index, reward = row
    options = json.loads(opts_json)
    return {"id": qid, "q": qtext, "options": options, "answer_index": answer_index, "reward": float(reward)}

async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    q = get_random_quiz_question(uid)
    if not q:
        await update.message.reply_text("❗ Hozircha savollar mavjud emas. Keyinroq urinib ko'ring.")
        return ConversationHandler.END
    # send question with inline options
    buttons = [InlineKeyboardButton(opt, callback_data=f"quiz|{q['id']}|{i}") for i, opt in enumerate(q['options'])]
    kb = InlineKeyboardMarkup([buttons[i:i+1] for i in range(len(buttons))])  # one per line
    context.user_data['current_quiz'] = q
    await update.message.reply_text(f"❓ {q['q']}", reply_markup=kb)
    return QUIZ_ANSWER

async def quiz_answer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qobj = context.user_data.get('current_quiz')
    if not qobj:
        await update.callback_query.answer("❗ Vaqt o'tib ketgan yoki savol topilmadi.")
        return
    query = update.callback_query
    await query.answer()
    payload = query.data  # format: quiz|qid|selected_index
    try:
        _, qid_str, sel_str = payload.split("|")
        sel = int(sel_str)
    except:
        await query.edit_message_text("❗ Noto'g'ri ma'lumot.")
        return ConversationHandler.END
    correct = 1 if sel == qobj['answer_index'] else 0
    # store history
    cur.execute("INSERT INTO user_quiz_history (user_id, question_id, correct) VALUES (?, ?, ?)",
                (query.from_user.id, qobj['id'], correct))
    db_commit()
    if correct:
        reward = float(qobj['reward'])
        add_transaction(query.from_user.id, "quiz", reward, f"quiz q{qobj['id']}", {"question": qobj['id']})
        await query.edit_message_text(f"✅ To'g'ri! Siz {reward} rubl oldingiz.")
        notify_admin(context.application, f"🎓 Quiz: {query.from_user.id} got q{qobj['id']} correct. +{reward}")
    else:
        await query.edit_message_text("❌ Xato javob. Keyingi qiynog'ingizga omad tilaymiz!")
    # cleanup
    context.user_data.pop('current_quiz', None)
    return ConversationHandler.END

# ----- SPIN WHEEL -----
SPIN_CONFIRM = range(1)

def spin_rewards_table():
    # weighted rewards: reward -> weight
    return [
        (0.0, 10),  # nothing
        (0.2, 25),
        (0.5, 20),
        (1.0, 15),
        (2.0, 10),
        (5.0, 5),
        (10.0, 1)
    ]

def spin_once():
    table = spin_rewards_table()
    total_weight = sum(w for _, w in table)
    r = random.randint(1, total_weight)
    upto = 0
    for reward, weight in table:
        upto += weight
        if r <= upto:
            return float(reward)
    return 0.0

async def spin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid, update.message)
    # allow once per day free spin: check spins table for today
    today = datetime.date.today().isoformat()
    cur.execute("SELECT COUNT(*) FROM spins WHERE user_id=? AND DATE(ts)=?", (uid, today))
    cnt = cur.fetchone()[0]
    if cnt >= 1:
        await update.message.reply_text("⚠️ Bugun bepul spin allaqachon ishlatilgan. Keyingi spin uchun shopga qarang.")
        return
    # spin
    reward = spin_once()
    add_transaction(uid, "spin", reward, f"daily_spin", {"reward": reward})
    cur.execute("INSERT INTO spins (user_id, reward) VALUES (?, ?)", (uid, reward))
    db_commit()
    if reward > 0:
        await update.message.reply_text(f"🎉 Ajoyib! Siz {reward} rubl yutdingiz. Balans: {safe_round(get_balance(uid))} rubl")
    else:
        await update.message.reply_text("😕 Afsus, hech narsa yutmadingiz. Ertaga yana urinib ko'ring!")
    notify_admin(context.application, f"🎡 Spin: {uid} reward {reward}")

# ----- MISSIONS & SHOP -----
# simple mission example: follow X channels + like some short -> for now we implement simple tasks with progress manually
def create_sample_missions():
    # create if not exists
    cur.execute("SELECT COUNT(*) FROM missions")
    if cur.fetchone()[0] == 0:
        # mission 1: refer 1 friend
        cur.execute("INSERT INTO missions (code, title, description, reward, condition_json) VALUES (?, ?, ?, ?, ?)",
                    ("ref1", "Taklif et 1 do'st", "1 do'st taklif eting va +4 rubl oling", 4.0, json.dumps({"referrals":1})))
        # mission 2: daily spin (just example)
        cur.execute("INSERT INTO missions (code, title, description, reward, condition_json) VALUES (?, ?, ?, ?, ?)",
                    ("spin1", "Bepul Spin", "Bepul spin bajarish", 0.5, json.dumps({"spins":1})))
        db_commit()

def check_and_apply_missions_for_user(user_id: int):
    # naive implementation: check missions and grant reward if condition met and not yet completed
    cur.execute("SELECT id, condition_json, reward FROM missions")
    rows = cur.fetchall()
    for mid, cond_json, reward in rows:
        cond = json.loads(cond_json)
        # check if already completed
        cur.execute("SELECT completed FROM user_missions WHERE user_id=? AND mission_id=?", (user_id, mid))
        rr = cur.fetchone()
        if rr and rr[0]==1:
            continue
        ok = True
        # referrals condition
        if cond.get("referrals"):
            cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer=?", (user_id,))
            count = cur.fetchone()[0]
            if count < cond["referrals"]:
                ok = False
        if cond.get("spins"):
            cur.execute("SELECT COUNT(*) FROM spins WHERE user_id=?", (user_id,))
            sc = cur.fetchone()[0]
            if sc < cond["spins"]:
                ok = False
        # other conditions could be added
        if ok:
            # mark completed and reward
            cur.execute("INSERT OR REPLACE INTO user_missions (user_id, mission_id, progress_json, completed, completed_at) VALUES (?, ?, ?, 1, datetime('now'))",
                        (user_id, mid, json.dumps({"auto":True})))
            add_transaction(user_id, "mission_reward", reward, f"mission {mid}")
            notify_admin(None, f"🏅 Mission completed: user {user_id} mission {mid} reward {reward}")
    db_commit()

# ----- LEADERBOARD -----
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT id, username, balance FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()
    text = "🏆 Leaderboard (Top 10 by balance):\n"
    rank = 1
    for uid, username, bal in rows:
        uname = f"@{username}" if username else str(uid)
        text += f"{rank}. {uname} — {safe_round(bal)} rubl\n"
        rank += 1
    await update.message.reply_text(text)

# ----- ADMIN COMMANDS (simple) -----
async def admin_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only allow admin channel or developer (for simplicity allow chat id of ADMIN_CHANNEL is channel - can't check easily)
    # show basic stats
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT SUM(balance) FROM users")
    total_balance = cur.fetchone()[0] or 0.0
    cur.execute("SELECT COUNT(*) FROM orders WHERE status='new'")
    new_orders = cur.fetchone()[0]
    await update.message.reply_text(f"📊 Stats:\nUsers: {users_count}\nTotal virtual balance: {safe_round(total_balance)}\nNew orders: {new_orders}")

async def admin_add_balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /addbal <user_id> <amount>
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addbal <user_id> <amount>")
        return
    try:
        uid = int(context.args[0])
        amt = float(context.args[1])
    except:
        await update.message.reply_text("Invalid args")
        return
    ensure_user(uid)
    add_transaction(uid, "admin_adjust", amt, f"Admin adjustment by {update.effective_user.id}")
    await update.message.reply_text(f"✅ {amt} rubl qo'shildi user {uid}")

# ----- DAILY AUDIT (scheduler job) -----
async def daily_audit_job(app):
    """Run daily audits: missions, reset daily_sent, check manual penalties etc."""
    logger.info("Running daily audit job...")
    # reset daily_sent for all users
    cur.execute("UPDATE users SET daily_sent = 0")
    db_commit()
    # apply missions automatically
    cur.execute("SELECT id FROM users")
    rows = cur.fetchall()
    for (uid,) in rows:
        try:
            check_and_apply_missions_for_user(uid)
        except Exception as e:
            logger.error("Mission apply error for %s: %s", uid, e)
    # optionally run subscription checks (telegram channels) here (rate-limited)
    logger.info("Daily audit done.")

# --------------- SETUP SAMPLE DATA -----------------
def setup_sample_questions():
    # Only add sample if none exist
    cur.execute("SELECT COUNT(*) FROM quiz_questions")
    if cur.fetchone()[0] == 0:
        qlist = [
            ("O'zbekiston poytaxti qaysi?", ["Toshkent","Samarqand","Buxoro","Namangan"], 0, 0.5),
            ("Python qaysi yil paydo bo'ldi?", ["1989","1991","1994","2000"], 1, 0.5),
            ("Telegram kim tomonidan asos solingan?", ["Pavel Durov","Mark Zuckerberg","Elon Musk","Bill Gates"], 0, 0.5),
        ]
        for qtext, options, ans, reward in qlist:
            cur.execute("INSERT INTO quiz_questions (q, options, answer_index, reward) VALUES (?, ?, ?, ?)",
                        (qtext, json.dumps(options, ensure_ascii=False), ans, reward))
        db_commit()

# --------------- HANDLERS & ROUTING -----------------
def register_handlers(app):
    # start
    app.add_handler(CommandHandler("start", start_handler))
    # simple commands
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("transactions", transactions_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("admin_stats", admin_stats_cmd))
    app.add_handler(CommandHandler("addbal", admin_add_balance_cmd))
    # daily bonus text button maps
    app.add_handler(MessageHandler(filters.Regex("^🎁 Kunlik bonus$"), daily_bonus_cmd))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balansim$"), balance_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📥 Tranzaksiyalar$"), transactions_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🏆 Leaderboard$"), leaderboard_cmd))
    # Order flow: button triggers start
    app.add_handler(MessageHandler(filters.Regex("^📝 Buyurtma berish$|^Buyurtma berish$"), order_start))
    # handle any text for orders or fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, order_message_handler))
    # spin
    app.add_handler(MessageHandler(filters.Regex("^💠 Spin Wheel$|^Spin Wheel$"), spin_start))
    # referral link button
    app.add_handler(MessageHandler(filters.Regex("^🔗 Referal havola$"), start_handler))
    # shop placeholder
    app.add_handler(MessageHandler(filters.Regex("^🛒 Shop$"), lambda u, c: u.message.reply_text("Shop hozircha ochilmagan."))
    )

    # Conversation handlers
    # send money conv
    send_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Pul yuborish$"), send_start), CommandHandler("send", send_start)],
        states={
            SEND_RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_recipient)],
            SEND_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_amount)],
            SEND_CONFIRM: [CallbackQueryHandler(send_confirm, pattern="^(confirm_send|cancel_send)$")]
        },
        fallbacks=[],
        per_user=True
    )
    app.add_handler(send_conv)

    # quiz conv
    quiz_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎯 Daily Quiz$"), quiz_start), CommandHandler("quiz", quiz_start)],
        states={
            QUIZ_ASK: [],  # not used
            QUIZ_ANSWER: [CallbackQueryHandler(quiz_answer_cb, pattern="^quiz\\|")]
        },
        fallbacks=[],
        per_user=True
    )
    app.add_handler(quiz_conv)

    # spin direct command
    app.add_handler(CommandHandler("spin", spin_start))
    app.add_handler(CommandHandler("daily_bonus", daily_bonus_cmd))
    app.add_handler(CommandHandler("transactions", transactions_cmd))
    app.add_handler(CommandHandler("orders", lambda u,c: c.bot.send_message(u.effective_chat.id, "Orders admin panel not implemented.")))

# --------------- STARTUP -----------------
def main():
    # prepare sample data and missions
    setup_sample_questions()
    create_sample_missions()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # register handlers
    register_handlers(app)

    # scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: app.create_task(daily_audit_job(app)), 'cron', hour=0, minute=5)  # run daily at 00:05 UTC
    scheduler.start()

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
