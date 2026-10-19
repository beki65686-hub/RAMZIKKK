import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


DEFAULT_BROADCAST_NAME = "Ism"
DEFAULT_BROADCAST_PHONE = "+998"
DEFAULT_BROADCAST_MESSAGE = ""
BROADCAST_INTERVAL_SECONDS = 120
BROADCAST_INTERVAL_MINUTES = (2, 3, 4, 5, 6, 7, 8)
BROADCAST_STOP_WORDS = {"stop", "toxtat", "stopbroadcast", "/stopbroadcast", "/toxtat", "/osha"}
BROADCAST_START_WORDS = {"osha", "/osha"}
DEFAULT_PREMIUM_PRICES = {1: 100000, 3: 300000, 12: 1000000}
PREMIUM_PLAN_NAMES = {1: "1 oy", 3: "3 oy", 12: "12 oy"}
PRO_PLAN_DAYS = {"1d": 1, "7d": 7, "1m": 30, "3m": 90, "12m": 360}
PRO_PLAN_NAMES = {"1d": "1 kun", "7d": "1 hafta", "1m": "1 oy", "3m": "3 oy", "12m": "12 oy"}
PRO_PLAN_DEFAULT_PRICES = {"1d": 3333, "7d": 23333, "1m": 100000, "3m": 300000, "12m": 1000000}

def load_bot_token() -> str:
    for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()

    env_path = Path(__file__).resolve().with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            if key in {"TELEGRAM_BOT_TOKEN", "BOT_TOKEN"} and value:
                return value.strip().strip('"').strip("'")

    return ""


TOKEN = load_bot_token()
OWNER_ID = 8852837020  # o'zing Telegram ID-ingizni shu yerga yozing
DB_PATH = Path("allowed_users.db")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE allowed_users ADD COLUMN expires_at TEXT")
    except sqlite3.OperationalError:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id INTEGER PRIMARY KEY,
            premium_until TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS premium_requests (
            user_id INTEGER PRIMARY KEY,
            months INTEGER NOT NULL,
            duration_days INTEGER,
            name TEXT,
            phone TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT,
            approved_by INTEGER
        )
        """
    )
    try:
        cur.execute("ALTER TABLE premium_requests ADD COLUMN duration_days INTEGER")
    except sqlite3.OperationalError:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            phone TEXT,
            username TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE user_profiles ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_balances (
            user_id INTEGER PRIMARY KEY,
            amount INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            referred_user_id INTEGER PRIMARY KEY,
            referrer_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            rewarded_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_broadcasts (
            user_id INTEGER PRIMARY KEY,
            message TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL DEFAULT 2,
            is_active INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS group_message_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            sender_user_id INTEGER
        )
        """
    )
    try:
        cur.execute("ALTER TABLE group_message_sends ADD COLUMN sender_user_id INTEGER")
    except sqlite3.OperationalError:
        pass
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_message_sends_sent_at ON group_message_sends (sent_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_message_sends_sender_time ON group_message_sends (sender_user_id, sent_at)"
    )
    conn.commit()
    conn.close()


def record_group_message_sent(user_id: int, sent_at: datetime | None = None) -> None:
    sent_at = sent_at or datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO group_message_sends (sent_at, sender_user_id) VALUES (?, ?)",
        (sent_at.isoformat(), user_id),
    )
    cur.execute(
        "DELETE FROM group_message_sends WHERE sent_at < ?",
        ((sent_at - timedelta(days=7)).isoformat(),),
    )
    conn.commit()
    conn.close()


def get_group_message_stats(
    now: datetime | None = None,
    sender_user_id: int | None = None,
) -> tuple[int, int]:
    now = now or datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    def count_messages(days: int) -> int:
        query = "SELECT COUNT(*) FROM group_message_sends WHERE sent_at >= ? AND sent_at <= ?"
        params: list[str | int] = [
            (now - timedelta(days=days)).isoformat(),
            now.isoformat(),
        ]
        if sender_user_id is not None:
            query += " AND sender_user_id = ?"
            params.append(sender_user_id)
        cur.execute(query, params)
        return int(cur.fetchone()[0])

    last_day = count_messages(1)
    last_week = count_messages(7)
    conn.close()
    return last_day, last_week


def save_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
    conn.close()


def load_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return default
    return str(row[0])


def get_user_balance(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT amount FROM user_balances WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def add_user_balance(user_id: int, amount: int) -> int:
    if amount <= 0:
        raise ValueError("Balansga qo'shiladigan summa 0 dan katta bo'lishi kerak.")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_balances (user_id, amount, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            amount = user_balances.amount + excluded.amount,
            updated_at = excluded.updated_at
        """,
        (user_id, amount, datetime.now().isoformat()),
    )
    conn.commit()
    cur.execute("SELECT amount FROM user_balances WHERE user_id = ?", (user_id,))
    balance = int(cur.fetchone()[0])
    conn.close()
    return balance


def remove_user_balance(user_id: int, amount: int) -> int | None:
    if amount <= 0:
        raise ValueError("Balansdan olinadigan summa 0 dan katta bo'lishi kerak.")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_balances SET amount = amount - ?, updated_at = ? "
        "WHERE user_id = ? AND amount >= ?",
        (amount, datetime.now().isoformat(), user_id, amount),
    )
    if cur.rowcount == 0:
        conn.close()
        return None
    cur.execute("SELECT amount FROM user_balances WHERE user_id = ?", (user_id,))
    balance = int(cur.fetchone()[0])
    conn.commit()
    conn.close()
    return balance


def parse_balance_adjustment(value: str) -> tuple[int, int] | None:
    parts = value.split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    amount_text = parts[1].replace(".", "")
    if not amount_text.isdigit():
        return None
    user_id, amount = int(parts[0]), int(amount_text)
    if user_id <= 0 or amount <= 0:
        return None
    return user_id, amount


def parse_balance_target(value: str) -> tuple[str, int] | None:
    parts = value.split()
    if len(parts) != 2:
        return None
    target = parts[0]
    amount_text = parts[1].replace(".", "")
    if not amount_text.isdigit() or int(amount_text) <= 0:
        return None
    if target.isdigit():
        if int(target) <= 0:
            return None
    else:
        username = target.removeprefix("@").strip()
        if not username or not all(character.isalnum() or character == "_" for character in username):
            return None
    return target, int(amount_text)


def resolve_balance_user_id(target: str) -> int | None:
    if target.isdigit():
        user_id = int(target)
        return user_id if user_id > 0 else None

    username = target.removeprefix("@").strip()
    if not username:
        return None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM user_profiles WHERE username = ? COLLATE NOCASE "
        "OR username = ? COLLATE NOCASE",
        (username, f"@{username}"),
    )
    rows = cur.fetchall()
    conn.close()
    if len(rows) != 1:
        return None
    return int(rows[0][0])


def get_referral_bonus_amount() -> int:
    try:
        return max(0, int(load_setting("referral_bonus", "10000")))
    except ValueError:
        return 10000


def create_referral(referrer_user_id: int, referred_user_id: int) -> bool:
    if referrer_user_id <= 0 or referrer_user_id == referred_user_id:
        return False
    _, phone = load_user_profile(referred_user_id)
    if has_registered_phone(phone):
        return False

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO referrals (referred_user_id, referrer_user_id, created_at) VALUES (?, ?, ?)",
        (referred_user_id, referrer_user_id, datetime.now().isoformat()),
    )
    created = cur.rowcount > 0
    conn.commit()
    conn.close()
    return created


def capture_referral_argument(referred_user_id: int, argument: str) -> bool:
    if not argument.startswith("ref_"):
        return False
    try:
        referrer_user_id = int(argument.removeprefix("ref_"))
    except ValueError:
        return False
    return create_referral(referrer_user_id, referred_user_id)


def get_referral_stats(user_id: int) -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), SUM(CASE WHEN rewarded_at IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM referrals WHERE referrer_user_id = ?",
        (user_id,),
    )
    total, rewarded = cur.fetchone()
    conn.close()
    return int(total or 0), int(rewarded or 0)


def reward_referrer_on_pro_activation(referred_user_id: int) -> tuple[int, int, int] | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        "SELECT referrer_user_id, rewarded_at FROM referrals WHERE referred_user_id = ?",
        (referred_user_id,),
    )
    row = cur.fetchone()
    if row is None or row[1] is not None:
        conn.rollback()
        conn.close()
        return None

    referrer_user_id = int(row[0])
    cur.execute("SELECT value FROM settings WHERE key = 'referral_bonus'")
    bonus_row = cur.fetchone()
    try:
        bonus = max(0, int(bonus_row[0])) if bonus_row else 10000
    except ValueError:
        bonus = 10000

    now_text = datetime.now().isoformat()
    if bonus:
        cur.execute(
            """
            INSERT INTO user_balances (user_id, amount, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                amount = user_balances.amount + excluded.amount,
                updated_at = excluded.updated_at
            """,
            (referrer_user_id, bonus, now_text),
        )
    cur.execute(
        "UPDATE referrals SET rewarded_at = ? WHERE referred_user_id = ? AND rewarded_at IS NULL",
        (now_text, referred_user_id),
    )
    cur.execute("SELECT amount FROM user_balances WHERE user_id = ?", (referrer_user_id,))
    balance_row = cur.fetchone()
    balance = int(balance_row[0]) if balance_row else 0
    conn.commit()
    conn.close()
    return referrer_user_id, bonus, balance

async def notify_referrer_of_pro_activation(
    context: ContextTypes.DEFAULT_TYPE,
    referred_user_id: int,
) -> None:
    reward = reward_referrer_on_pro_activation(referred_user_id)
    if reward is None:
        return
    referrer_user_id, bonus, balance = reward
    if bonus <= 0:
        return
    try:
        await context.bot.send_message(
            chat_id=referrer_user_id,
            text=(
                "Do'stingiz Pro sotib oldi 🎉\n"
                f"Referral bonusi: {format_price(bonus)} so'm\n"
                f"Balansingiz: {format_price(balance)} so'm"
            ),
        )
    except Exception:
        logging.info("Referral bonus notification could not be sent to user %s", referrer_user_id)


def normalize_plan_id(plan: str | int) -> str:
    plan_id = f"{plan}m" if isinstance(plan, int) else plan
    if plan_id not in PRO_PLAN_DAYS:
        raise ValueError("Noma'lum Pro paketi.")
    return plan_id


def parse_pro_duration(value: str) -> int | None:
    text = value.strip().lower()
    direct_days = {"1d": 1, "1w": 7, "1week": 7, "hafta": 7, "1m": 30, "3m": 90, "12m": 360}
    if text in direct_days:
        return direct_days[text]
    if text.isdigit() and int(text) > 0:
        return int(text) * 30
    return None


def get_pro_price(plan: str | int) -> int:
    plan_id = normalize_plan_id(plan)
    legacy_months = {"1m": 1, "3m": 3, "12m": 12}.get(plan_id)
    setting_key = f"premium_price_{legacy_months}" if legacy_months else f"pro_price_{plan_id}"
    default_price = PRO_PLAN_DEFAULT_PRICES[plan_id]
    value = load_setting(setting_key, str(default_price))
    try:
        return int(value)
    except ValueError:
        return default_price


def set_pro_price(plan: str | int, price: int) -> None:
    plan_id = normalize_plan_id(plan)
    if price <= 0:
        raise ValueError("Narx 0 dan katta bo'lishi kerak.")
    legacy_months = {"1m": 1, "3m": 3, "12m": 12}.get(plan_id)
    setting_key = f"premium_price_{legacy_months}" if legacy_months else f"pro_price_{plan_id}"
    save_setting(setting_key, str(price))


def get_premium_price(months: int) -> int:
    return get_pro_price(months)


def set_premium_price(months: int, price: int) -> None:
    set_pro_price(months, price)


def format_price(price: int) -> str:
    return f"{price:,}".replace(",", ".")


def premium_prices_text() -> str:
    return "\n".join(
        f"{PRO_PLAN_NAMES[plan_id]} - {format_price(get_pro_price(plan_id))}"
        for plan_id in PRO_PLAN_DAYS
    )


def parse_premium_price(value: str) -> int | None:
    normalized = value.strip().replace(".", "").replace(" ", "")
    if not normalized.isdigit():
        return None
    price = int(normalized)
    return price if price > 0 else None


def load_allowed_users() -> set[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at FROM allowed_users")
    rows = cur.fetchall()
    conn.close()
    allowed = {OWNER_ID}
    now = datetime.now()
    for user_id, expires_at in rows:
        if expires_at is None:
            allowed.add(int(user_id))
            continue
        try:
            if datetime.fromisoformat(str(expires_at)) > now:
                allowed.add(int(user_id))
        except ValueError:
            continue
    return allowed


def add_allowed_user(user_id: int, days: float | None = None) -> datetime | None:
    expiry = None
    if days is not None:
        expiry = datetime.now() + timedelta(days=days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO allowed_users (user_id, expires_at) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at",
        (user_id, expiry.isoformat() if expiry else None),
    )
    conn.commit()
    conn.close()
    return expiry


def parse_access_duration(value: str) -> float | None:
    text = value.strip().lower()
    if text.endswith("h"):
        try:
            return float(text[:-1]) / 24
        except ValueError:
            return None
    if text.endswith("m"):
        try:
            return float(text[:-1]) / (24 * 60)
        except ValueError:
            return None
    if text.endswith("d"):
        try:
            return float(text[:-1])
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def remove_allowed_user(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def save_premium_expiry(user_id: int, months: int) -> datetime:
    expires_at = datetime.now() + timedelta(days=30 * months)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO premium_users (user_id, premium_until) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET premium_until = excluded.premium_until",
        (user_id, expires_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return expires_at


def grant_premium_for_user(user_id: int, months: int) -> datetime:
    if months <= 0:
        raise ValueError("Pro muddati 0 dan katta bo'lishi kerak.")

    return grant_pro_for_days(user_id, months * 30)


def grant_pro_for_days(user_id: int, duration_days: int) -> datetime:
    if duration_days <= 0:
        raise ValueError("Pro muddati 0 dan katta bo'lishi kerak.")

    current_expiry = get_premium_expiry(user_id)
    if current_expiry is not None and current_expiry > datetime.now():
        expires_at = current_expiry + timedelta(days=duration_days)
    else:
        expires_at = datetime.now() + timedelta(days=duration_days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO premium_users (user_id, premium_until) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET premium_until = excluded.premium_until",
        (user_id, expires_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return expires_at


def purchase_pro_with_balance(
    user_id: int,
    plan: str | int,
) -> tuple[bool, datetime | None, int, int]:
    plan_id = normalize_plan_id(plan)
    price = get_pro_price(plan_id)
    duration_days = PRO_PLAN_DAYS[plan_id]
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("SELECT amount FROM user_balances WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    balance = int(row[0]) if row else 0
    if balance < price:
        conn.rollback()
        conn.close()
        return False, None, balance, price

    cur.execute("SELECT premium_until FROM premium_users WHERE user_id = ?", (user_id,))
    expiry_row = cur.fetchone()
    current_expiry = None
    if expiry_row is not None:
        try:
            current_expiry = datetime.fromisoformat(str(expiry_row[0]))
        except ValueError:
            current_expiry = None
    start_date = current_expiry if current_expiry and current_expiry > now else now
    expires_at = start_date + timedelta(days=duration_days)

    cur.execute(
        "UPDATE user_balances SET amount = amount - ?, updated_at = ? WHERE user_id = ?",
        (price, now.isoformat(), user_id),
    )
    cur.execute(
        "INSERT INTO premium_users (user_id, premium_until) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET premium_until = excluded.premium_until",
        (user_id, expires_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return True, expires_at, balance - price, price


def revoke_premium_access(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
    removed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def get_premium_expiry(user_id: int) -> datetime | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT premium_until FROM premium_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(str(row[0]))
    except ValueError:
        return None


def format_pro_remaining(user_id: int, now: datetime | None = None) -> str:
    if user_id == OWNER_ID:
        return "Muddatsiz (admin)"
    expiry = get_premium_expiry(user_id)
    if expiry is None:
        return "Faol Pro yo'q"
    if now is None:
        now = datetime.now()
    remaining = expiry - now
    if remaining.total_seconds() <= 0:
        return "Muddati tugagan"

    hours_total, seconds = divmod(int(remaining.total_seconds()), 3600)
    days, hours = divmod(hours_total, 24)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days} kun")
    if hours:
        parts.append(f"{hours} soat")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} daqiqa")
    if not parts:
        parts.append("1 daqiqadan kam")
    remaining_text = " ".join(parts[:2])
    return f"{remaining_text} (tugash: {expiry.strftime('%d.%m.%Y %H:%M')})"


def list_active_premium_users() -> list[tuple[int, str, str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT premium_users.user_id, user_profiles.username, user_profiles.name,
               user_profiles.phone, premium_users.premium_until
        FROM premium_users
        LEFT JOIN user_profiles ON user_profiles.user_id = premium_users.user_id
        WHERE premium_users.premium_until > ?
        ORDER BY premium_users.premium_until DESC
        """,
        (datetime.now().isoformat(),),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        (int(user_id), str(username or ""), str(name or ""), str(phone or ""), str(premium_until))
        for user_id, username, name, phone, premium_until in rows
    ]


def save_premium_request(user_id: int, plan: str | int, name: str = "", phone: str = "") -> None:
    plan_id = normalize_plan_id(plan)
    duration_days = PRO_PLAN_DAYS[plan_id]
    legacy_months = {"1m": 1, "3m": 3, "12m": 12}.get(plan_id, 0)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO premium_requests (user_id, months, duration_days, name, phone, status, requested_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            months = excluded.months,
            duration_days = excluded.duration_days,
            name = excluded.name,
            phone = excluded.phone,
            status = 'pending',
            requested_at = excluded.requested_at,
            approved_at = NULL,
            approved_by = NULL
        """,
        (user_id, legacy_months, duration_days, name, phone, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def list_pending_premium_requests() -> list[tuple[int, int, int | None, str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, months, duration_days, name, phone, requested_at "
        "FROM premium_requests WHERE status = 'pending' ORDER BY requested_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def approve_premium_request(user_id: int, approved_by: int = OWNER_ID) -> tuple[bool, datetime | None]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT months, duration_days FROM premium_requests WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False, None
    duration_days = int(row[1]) if row[1] is not None else int(row[0]) * 30
    expires_at = grant_pro_for_days(user_id, duration_days)
    cur.execute(
        "UPDATE premium_requests SET status = 'approved', approved_at = ?, approved_by = ? WHERE user_id = ?",
        (datetime.now().isoformat(), approved_by, user_id),
    )
    conn.commit()
    conn.close()
    return True, expires_at


def reject_premium_request(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM premium_requests WHERE user_id = ? AND status = 'pending'", (user_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False
    cur.execute("UPDATE premium_requests SET status = 'rejected', approved_at = ?, approved_by = ? WHERE user_id = ?", (datetime.now().isoformat(), OWNER_ID, user_id))
    conn.commit()
    conn.close()
    return True


def clear_expired_premium_users(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM premium_users WHERE premium_until <= ?", (now.isoformat(),))
    conn.commit()
    conn.close()


def clear_expired_allowed_users(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM allowed_users WHERE expires_at IS NOT NULL AND expires_at <= ?", (now.isoformat(),))
    conn.commit()
    conn.close()


def is_premium_active(user_id: int, now: datetime | None = None) -> bool:
    if user_id == OWNER_ID:
        return True
    if now is None:
        now = datetime.now()
    expiry = get_premium_expiry(user_id)
    return expiry is not None and expiry > now


def save_user_profile(
    user_id: int,
    name: str | None = None,
    phone: str | None = None,
    username: str | None = None,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name, phone, username FROM user_profiles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    current_name, current_phone, current_username = row or ("", "", "")

    if name is None:
        name = current_name
    if phone is None:
        phone = current_phone
    if username is None:
        username = current_username

    cur.execute(
        "INSERT INTO user_profiles (user_id, name, phone, username) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, phone = excluded.phone, username = excluded.username",
        (user_id, name, phone, username),
    )
    conn.commit()
    conn.close()


def load_user_profile(user_id: int) -> tuple[str, str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name, phone FROM user_profiles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return ("", "")
    return (str(row[0] or ""), str(row[1] or ""))


def load_user_username(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT username FROM user_profiles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return str(row[0] or "") if row else ""


def has_registered_phone(phone: str) -> bool:
    return sum(character.isdigit() for character in phone) >= 7


def build_phone_request_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton("📞 Telefon raqamni yuborish", request_contact=True)]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def parse_required_channel(value: str) -> tuple[str, str] | None:
    parts = value.strip().split(maxsplit=1)
    if not parts:
        return None

    channel = parts[0]
    if channel.startswith("@") and len(channel) > 1:
        join_url = parts[1] if len(parts) == 2 else f"https://t.me/{channel[1:]}"
    elif channel.startswith("-100") and channel[1:].isdigit() and len(parts) == 2:
        join_url = parts[1]
    else:
        return None

    if not join_url.startswith(("https://t.me/", "https://telegram.me/")):
        return None
    return channel, join_url


def build_channel_subscription_keyboard(
    channel_link: str = "",
    group_link: str = "",
) -> InlineKeyboardMarkup:
    keyboard = []
    if channel_link:
        keyboard.append([InlineKeyboardButton("📢 Kanalga obuna bo'lish", url=channel_link)])
    if group_link:
        keyboard.append([InlineKeyboardButton("👥 Guruhga qo'shilish", url=group_link)])
    keyboard.append([InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_channel_subscription")])
    return InlineKeyboardMarkup(
        keyboard
    )


async def is_user_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    required_chats = (
        load_setting("required_channel", "").strip(),
        load_setting("required_group", "").strip(),
    )
    for chat_id in required_chats:
        if not chat_id:
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception:
            logging.exception("Could not verify required chat membership for user %s", user_id)
            return False
        status = getattr(member, "status", "")
        if status not in {"member", "administrator", "creator"} and not (
            status == "restricted" and bool(getattr(member, "is_member", False))
        ):
            return False
    return True


async def continue_after_channel_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = update.callback_query
    name, phone = load_user_profile(user.id)
    if not has_registered_phone(phone):
        await query.edit_message_text("Majburiy obuna tasdiqlandi ✅")
        await context.bot.send_message(
            chat_id=user.id,
            text="Botdan foydalanish uchun telefon raqamingizni yuboring.",
            reply_markup=build_phone_request_keyboard(),
        )
        return

    save_user_profile(user.id, name=name or user.full_name, username=user.username)
    if not is_allowed(user.id):
        await query.edit_message_text(
            f"Majburiy obuna tasdiqlandi ✅\n\nPro paketni tanlang:\n\n{premium_prices_text()}",
            reply_markup=build_premium_menu(),
        )
        return

    await query.edit_message_text(
        "Majburiy obuna tasdiqlandi ✅ Asosiy menyu:",
        reply_markup=build_main_menu(is_admin=user.id == OWNER_ID),
    )


async def required_channel_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id == OWNER_ID:
        return

    message = update.effective_message
    if message is not None and isinstance(message.text, str) and message.text:
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) == 2 and command_parts[0].split("@", 1)[0] == "/start":
            capture_referral_argument(user.id, command_parts[1])

    channel = load_setting("required_channel", "").strip()
    group = load_setting("required_group", "").strip()
    if not channel and not group:
        return

    query = update.callback_query
    if query is not None and query.data == "check_channel_subscription":
        if await is_user_subscribed(context, user.id):
            await query.answer("Obuna tasdiqlandi ✅")
            await continue_after_channel_check(update, context)
        else:
            await query.answer(
                "Majburiy kanal yoki guruhga a'zolik aniqlanmadi.",
                show_alert=True,
            )
        raise ApplicationHandlerStop

    if await is_user_subscribed(context, user.id):
        return

    channel_link = load_setting("required_channel_link", "")
    group_link = load_setting("required_group_link", "")
    message = update.effective_message
    if message is not None:
        prompt = (
            "Botdan foydalanish uchun ko'rsatilgan kanal va guruhlarga a'zo bo'ling, "
            "so'ng «Obuna bo'ldim» tugmasini bosing."
        )
        if query is not None:
            await query.answer("Avval kanalga obuna bo'ling.", show_alert=True)
        await message.reply_text(
            prompt,
            reply_markup=build_channel_subscription_keyboard(channel_link, group_link),
        )
    raise ApplicationHandlerStop


async def registration_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    _, phone = load_user_profile(user.id)
    if has_registered_phone(phone):
        save_user_profile(user.id, username=user.username)
        return

    message = update.effective_message
    contact = message.contact if message is not None else None
    if contact is not None and contact.user_id == user.id:
        return

    prompt = "Avval telefon raqamingizni yuborib ro'yxatdan o'ting."
    if update.callback_query is not None:
        await update.callback_query.answer(prompt, show_alert=True)
        if message is not None:
            await message.reply_text(prompt, reply_markup=build_phone_request_keyboard())
    elif message is not None:
        await message.reply_text(prompt, reply_markup=build_phone_request_keyboard())
    raise ApplicationHandlerStop


def is_allowed(user_id: int) -> bool:
    clear_expired_premium_users()
    return user_id == OWNER_ID or is_premium_active(user_id)


def build_broadcast_message(
    user_id: int | str,
    phone: str,
    message: str,
    username: str = "",
) -> str:
    if message is None:
        return ""
    text = str(message).strip()
    if not text:
        return ""
    phone_digits = "".join(character for character in str(phone or "") if character.isdigit())
    phone_text = f"+{phone_digits}" if phone_digits else "+998"
    username_text = f"@{username.lstrip('@')}" if username else f"ID: {user_id}"
    return f"{text}\n\nTelefon: {phone_text}\nFoydalanuvchi: {username_text}"


async def send_periodic_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = getattr(context, "job", None)
    job_data = getattr(job, "data", None)
    if isinstance(job_data, dict) and job_data.get("user_id") is not None:
        await send_user_periodic_broadcast(context, int(job_data["user_id"]))
        return

    group_id = load_setting("group_chat_id", "")
    if not group_id:
        logging.warning("Broadcast skipped: group_chat_id not set")
        return

    message = load_setting("broadcast_message", "")
    user_id = load_setting("broadcast_user_id", "")
    try:
        broadcaster_id = int(user_id)
    except ValueError:
        logging.warning("Broadcast stopped: sender ID is missing or invalid")
        stop_broadcast_job(context)
        return
    if not is_allowed(broadcaster_id):
        logging.info("Broadcast stopped: sender %s no longer has premium access", broadcaster_id)
        stop_broadcast_job(context)
        return

    _, profile_phone = load_user_profile(broadcaster_id)
    phone = profile_phone or load_setting("broadcast_phone", "+998")
    username = load_user_username(broadcaster_id)
    text = build_broadcast_message(broadcaster_id, phone, message, username)
    if not text:
        logging.warning("Broadcast skipped: empty message payload")
        return
    try:
        logging.info("Sending scheduled broadcast to group %s", group_id)
        await context.bot.send_message(chat_id=int(group_id), text=text)
    except Exception:
        logging.exception("Scheduled broadcast failed")
    else:
        record_group_message_sent(broadcaster_id)


async def send_user_periodic_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT message, is_active FROM user_broadcasts WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None or not row[1]:
        stop_user_broadcast_job(context, user_id)
        return
    if not is_allowed(user_id):
        logging.info("User broadcast stopped: sender %s no longer has Pro", user_id)
        stop_user_broadcast_job(context, user_id)
        return

    group_id = load_setting("group_chat_id", "").strip()
    if not group_id:
        stop_user_broadcast_job(context, user_id)
        return

    _, phone = load_user_profile(user_id)
    text = build_broadcast_message(user_id, phone, str(row[0]), load_user_username(user_id))
    if not text:
        stop_user_broadcast_job(context, user_id)
        return
    try:
        await context.bot.send_message(chat_id=int(group_id), text=text)
    except Exception:
        logging.exception("User broadcast failed for user %s", user_id)
    else:
        record_group_message_sent(user_id)


def build_unpaid_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💎 Pro sotib olish", callback_data="premium")],
        [InlineKeyboardButton("💰 Balans", callback_data="balance")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("👤 Profil", callback_data="profile")],
        [
            InlineKeyboardButton("▶️ Boshlash", callback_data="start_broadcast"),
            InlineKeyboardButton("🛑 To'xtatish", callback_data="stop_broadcast"),
        ],
        [
            InlineKeyboardButton("⏱ Interval", callback_data="interval"),
            InlineKeyboardButton("💎 Pro", callback_data="premium"),
        ],
        [InlineKeyboardButton("💰 Balans", callback_data="balance")],
        [InlineKeyboardButton("📊 Statistika", callback_data="user_stats")],
        [
            InlineKeyboardButton("📨 Referral", callback_data="referral"),
        ],
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin")])
    return InlineKeyboardMarkup(keyboard)


def build_broadcast_interval_menu(selected_minutes: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if minutes == selected_minutes else ''}{minutes} daqiqa",
                callback_data=f"set_broadcast_interval_{minutes}",
            )
        ]
        for minutes in BROADCAST_INTERVAL_MINUTES
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


def get_user_broadcast_interval(user_id: int) -> int:
    try:
        minutes = int(load_setting(f"broadcast_interval_{user_id}", "2"))
    except ValueError:
        return 2
    return minutes if minutes in BROADCAST_INTERVAL_MINUTES else 2


def set_user_broadcast_interval(user_id: int, minutes: int) -> None:
    if minutes not in BROADCAST_INTERVAL_MINUTES:
        raise ValueError("Noto'g'ri interval.")
    save_setting(f"broadcast_interval_{user_id}", str(minutes))


def build_premium_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("⚡ 1 kun", callback_data="plan_1d"),
            InlineKeyboardButton("📅 1 hafta", callback_data="plan_7d"),
        ],
        [
            InlineKeyboardButton("💳 1 oy", callback_data="plan_1m"),
            InlineKeyboardButton("💎 3 oy", callback_data="plan_3m"),
        ],
        [InlineKeyboardButton("🏆 12 oy", callback_data="plan_12m")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_pro_purchase_menu(plan: str | int, balance: int) -> InlineKeyboardMarkup:
    plan_id = normalize_plan_id(plan)
    keyboard = [
        [
            InlineKeyboardButton(
                f"💰 Balansdan ({format_price(balance)} so'm)",
                callback_data=f"buy_balance_{plan_id}",
            )
        ],
        [InlineKeyboardButton("👨‍💼 Admin orqali", callback_data=f"buy_admin_{plan_id}")],
        [InlineKeyboardButton("⬅️ Paketlar", callback_data="premium")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_premium_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✅ Qabul qilish", callback_data=f"premium_approve_{user_id}")],
        [InlineKeyboardButton("❌ Rad etish", callback_data=f"premium_reject_{user_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Pro narxlari", callback_data="admin_prices")],
            [InlineKeyboardButton("👥 Pro foydalanuvchilar", callback_data="admin_premium_users")],
            [InlineKeyboardButton("💰 Balans qo'shish", callback_data="admin_add_balance")],
            [InlineKeyboardButton("➖ Balansni olib qo'yish", callback_data="admin_remove_balance")],
            [InlineKeyboardButton("🎁 Referral bonusi", callback_data="admin_referral_bonus")],
            [InlineKeyboardButton("📊 Guruh statistikasi", callback_data="stats")],
            [InlineKeyboardButton("📢 Majburiy kanal", callback_data="admin_channel")],
        ]
    )


def build_premium_user_actions(
    user_ids: list[int],
    completed_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    completed_ids = completed_ids or set()
    keyboard = []
    for user_id in user_ids:
        if user_id in completed_ids:
            keyboard.append(
                [InlineKeyboardButton("✅ Ruxsat olib tashlandi", callback_data=f"premium_revoke_done_{user_id}")]
            )
        else:
            keyboard.append(
                [InlineKeyboardButton("🚫 Ruxsatni olib qo'yish", callback_data=f"premium_revoke_{user_id}")]
            )
    return InlineKeyboardMarkup(keyboard)


def build_admin_prices_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                f"{PRO_PLAN_NAMES[plan_id]} - {format_price(get_pro_price(plan_id))}",
                callback_data=f"admin_price_{plan_id}",
            )
        ]
        for plan_id in PRO_PLAN_DAYS
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Admin paneli", callback_data="admin")])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    start_args = getattr(context, "args", None)
    if isinstance(start_args, (list, tuple)) and start_args and start_args[0].startswith("ref_"):
        capture_referral_argument(user_id, start_args[0])

    name, phone = load_user_profile(user_id)
    save_user_profile(user_id, username=update.effective_user.username)
    if not has_registered_phone(phone):
        await update.message.reply_text(
            "<b>Botga kirish uchun telefon raqamingizni yuboring.</b>\n\n"
            "Telefon raqamingiz yuborilmaguncha botdan foydalanish mumkin emas.",
            reply_markup=build_phone_request_keyboard(),
            parse_mode="HTML",
        )
        return

    if not is_allowed(user_id):
        await update.message.reply_text(
            "<b>Auto xabar bot</b>\n\n"
            "Kechirasiz, sizga hozircha kirish taqiqlangan.\n"
            "Botdan foydalanish uchun Pro paketni sotib oling.\n\n"
            "💎 Tariflar:\n"
            f"{premium_prices_text()}\n\n"
            "Pro muddati tugagandan keyin avtomatik ravishda kirish yopiladi.",
            reply_markup=build_unpaid_menu(),
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "<b>Auto xabar bot</b>\n\n"
        "Botda quyidagilar mavjud:\n"
        "• avtomatik xabarlar\n"
        "• profil va statistika\n"
        "• interval\n"
        "• pro va referral\n\n"
        "<b>Admin:</b> @kbrsz_aka",
        reply_markup=build_main_menu(is_admin=user_id == OWNER_ID),
        parse_mode="HTML",
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text(
            "Kechirasiz, sizga hozircha kirish taqiqlangan.\n"
            "Pro paketni sotib olib, botdan foydalaning.",
            reply_markup=build_unpaid_menu(),
        )
        return

    await update.message.reply_text(
        "Asosiy menyu",
        reply_markup=build_main_menu(is_admin=user_id == OWNER_ID),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text(
            "Kechirasiz, sizga hozircha kirish taqiqlangan.\n"
            "Pro paketni sotib olib, botdan foydalaning.",
            reply_markup=build_unpaid_menu(),
        )
        return

    await update.message.reply_text(
        "Mavjud buyruqlar:\n"
        "/start - boshlash\n"
        "/osha - avtomatik yuborishni yoqish\n"
        "/toxtat - avtomatik yuborishni to'xtatish\n"
        "/menu - menyu\n"
        "/help - yordam\n"
        "/status - holat\n"
        "/pro - Pro paketlar\n"
        "/admin - admin paneli\n"
        "/pro_requests - kutilayotgan so'rovlar\n"
        "/approve_pro 123456 1 - tasdiqlash\n"
        "/reject_pro 123456 - rad etish\n"
        "/grant_pro 123456 1d|1w|1m|3m - Pro berish\n"
        "/revoke_pro 123456 - Pro ruxsatini bekor qilish"
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>💎 Pro paketlar</b>\n\n"
        f"{premium_prices_text()}\n\n"
        "Pro foydalanuvchi:\n"
        "• cheksiz xabarlar\n"
        "• VIP statistikalar\n"
        "• qo'shimcha guruhlar\n"
        "• priority yordam",
        reply_markup=build_premium_menu(),
        parse_mode="HTML",
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Kechirasiz, faqat admin buyruqlarni ishlata oladi.")
        return

    await update.message.reply_text(
        "<b>⚙️ Admin panel</b>\n\n"
        "Buyruqlar:\n"
        "/setgroup 123456789\n"
        "/setname Ali\n"
        "/setphone +998901234567\n"
        "/setmessage Salom!\n"
        "/osha\n"
        "/toxtat\n"
        "/startbroadcast\n"
        "/stopbroadcast\n"
        "/status\n"
        "/pro_requests\n"
        "/approve_pro 123456 1\n"
        "/reject_pro 123456\n"
        "/grant_pro 123456 1d|1w|1m|3m\n"
        "/revoke_pro 123456\n\n"
        "Har 2 daqiqada xabar guruhga yuboriladi.",
        reply_markup=build_admin_menu(),
        parse_mode="HTML",
    )


async def premium_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin Pro so'rovlarini ko'rishi mumkin.")
        return
    rows = list_pending_premium_requests()
    if not rows:
        await update.message.reply_text("Hozirda kutilayotgan Pro so'rovlari yo'q ✅")
        return
    lines = []
    for user_id, months, duration_days, name, phone, requested_at in rows:
        name_text = name or "Noma'lum"
        phone_text = phone or "telefon yo'q"
        if duration_days is None:
            duration_text = PRO_PLAN_NAMES.get({1: "1m", 3: "3m", 12: "12m"}.get(months, ""), f"{months} oy")
        else:
            duration_text = {1: "1 kun", 7: "1 hafta", 30: "1 oy", 90: "3 oy", 360: "12 oy"}.get(
                duration_days,
                f"{duration_days} kun",
            )
        lines.append(f"- {user_id} | {duration_text} | {name_text} | {phone_text} | {requested_at}")
    await update.message.reply_text("Kutilayotgan Pro so'rovlar:\n" + "\n".join(lines))


async def approve_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin tasdiqlashi mumkin.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /approve_premium 123456 1")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID raqam bo'lishi kerak.")
        return
    months = 1
    if len(context.args) > 1:
        try:
            months = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Oy soni raqam bo'lishi kerak.")
            return
    ok, expires_at = approve_premium_request(user_id, OWNER_ID)
    if not ok:
        await update.message.reply_text(f"{user_id} uchun kutilayotgan Pro so'rovi topilmadi.")
        return
    await notify_referrer_of_pro_activation(context, user_id)
    await update.message.reply_text(
        f"{user_id} foydalanuvchi uchun Pro tasdiqlandi ✅\n"
        f"Muddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha"
    )


async def reject_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin rad etishi mumkin.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /reject_premium 123456")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID raqam bo'lishi kerak.")
        return
    if not reject_premium_request(user_id):
        await update.message.reply_text(f"{user_id} uchun Pro so'rovi topilmadi.")
        return
    await update.message.reply_text(f"{user_id} foydalanuvchining Pro so'rovi rad etildi ❌")


async def grant_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin Pro bera oladi.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /grant_pro 123456 1d|1w|1m|3m")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID raqam bo'lishi kerak.")
        return
    duration = parse_pro_duration(context.args[1]) if len(context.args) > 1 else 30
    if duration is None:
        await update.message.reply_text("Muddatni 1d, 1w, 1m yoki oy soni bilan kiriting.")
        return
    expires_at = grant_pro_for_days(user_id, duration)
    await update.message.reply_text(
        f"{user_id} foydalanuvchisiga {duration} kunlik Pro tayinlandi ✅\n"
        f"Muddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha"
    )


async def revoke_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin Pro ruxsatini olib tashlashi mumkin.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /revoke_premium 123456")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID raqam bo'lishi kerak.")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"{user_id} foydalanuvchisining Pro muddati bekor qilindi ❌")


async def set_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin o'zgartira oladi.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /setgroup -1001234567890")
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Guruh ID raqam bo'lishi kerak.")
        return
    save_setting("group_chat_id", str(group_id))
    await update.message.reply_text(f"Guruh ID saqlandi: {group_id}")


async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin o'zgartira oladi.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /setname Ali")
        return
    name = " ".join(context.args)
    save_setting("broadcast_name", name)
    await update.message.reply_text(f"Name saqlandi: {name}")


async def set_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin o'zgartira oladi.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /setphone +998901234567")
        return
    phone = " ".join(context.args)
    save_setting("broadcast_phone", phone)
    await update.message.reply_text(f"Phone saqlandi: {phone}")


async def set_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin o'zgartira oladi.")
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /setmessage Salom")
        return
    message = " ".join(context.args)
    save_setting("broadcast_message", message)
    await update.message.reply_text(f"Xabar saqlandi: {message}")


def has_broadcast_job(context: ContextTypes.DEFAULT_TYPE, user_id: int | None = None) -> bool:
    job_name = f"user_broadcast_{user_id}" if user_id is not None else "broadcast_job"
    return bool(context.job_queue.get_jobs_by_name(job_name))


def start_user_broadcast_job(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    message: str,
    interval_minutes: int,
) -> bool:
    clean_message = message.strip()
    if not clean_message or interval_minutes not in BROADCAST_INTERVAL_MINUTES:
        return False
    if not load_setting("group_chat_id", "").strip():
        return False

    stop_user_broadcast_job(context, user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_broadcasts (user_id, message, interval_minutes, is_active, updated_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            message = excluded.message,
            interval_minutes = excluded.interval_minutes,
            is_active = 1,
            updated_at = excluded.updated_at
        """,
        (user_id, clean_message, interval_minutes, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    context.job_queue.run_repeating(
        send_periodic_broadcast,
        interval=interval_minutes * 60,
        first=1,
        name=f"user_broadcast_{user_id}",
        data={"user_id": user_id},
    )
    return True


def stop_user_broadcast_job(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(f"user_broadcast_{user_id}"):
        job.schedule_removal()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE user_broadcasts SET is_active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def broadcast_is_ready() -> tuple[bool, str]:
    group_id = load_setting("group_chat_id", "").strip()
    if not group_id:
        return False, "Guruh ID belgilanmagan. Admin /setgroup bilan sozlang."

    message = load_setting("broadcast_message", "").strip()
    if not message:
        return False, "Avval xabar matni yozing. Keyin avtomatik yuborishni boshlash mumkin."

    return True, ""


def start_broadcast_job(context: ContextTypes.DEFAULT_TYPE, message: str | None = None) -> bool:
    text = (message or "").strip()
    if text:
        save_setting("broadcast_message", text)
    else:
        text = load_setting("broadcast_message", "").strip()

    if not text:
        return False

    if not load_setting("group_chat_id", "").strip():
        logging.warning("Broadcast skipped: group_chat_id not set")
        return False

    if has_broadcast_job(context):
        stop_broadcast_job(context)

    context.job_queue.run_repeating(
        send_periodic_broadcast,
        interval=BROADCAST_INTERVAL_SECONDS,
        first=1,
        name="broadcast_job",
    )
    save_setting("broadcast_active", "1")
    logging.info("Scheduled broadcast started; next send in 1s then every %ss", BROADCAST_INTERVAL_SECONDS)
    return True


def resume_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> bool:
    resumed = False
    if load_setting("broadcast_active", "0") == "1":
        try:
            broadcaster_id = int(load_setting("broadcast_user_id", ""))
        except ValueError:
            save_setting("broadcast_active", "0")
        else:
            if is_allowed(broadcaster_id):
                resumed = start_broadcast_job(context) or resumed
            else:
                save_setting("broadcast_active", "0")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, message, interval_minutes FROM user_broadcasts WHERE is_active = 1")
    user_broadcasts = cur.fetchall()
    conn.close()
    for user_id, message, interval_minutes in user_broadcasts:
        if is_allowed(int(user_id)):
            resumed = start_user_broadcast_job(
                context,
                int(user_id),
                str(message),
                int(interval_minutes),
            ) or resumed
        else:
            stop_user_broadcast_job(context, int(user_id))
    return resumed


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin boshqarishi mumkin.")
        return

    ready, reason = broadcast_is_ready()
    if not ready:
        await update.message.reply_text(reason)
        return

    if has_broadcast_job(context):
        await update.message.reply_text("Avtomatik xabar allaqachon ishlayapti ✅")
        return

    message = load_setting("broadcast_message", DEFAULT_BROADCAST_MESSAGE).strip()
    save_setting("broadcast_user_id", str(OWNER_ID))
    started = start_broadcast_job(context, message)
    if not started:
        await update.message.reply_text("Avtomatik yuborish ishga tushmadi. Guruh ID yoki xabar matni yo'q.")
        return

    await update.message.reply_text("Hamma 2 daqiqada xabar yuborish boshlandi ✅")


def stop_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for job in context.job_queue.get_jobs_by_name("broadcast_job"):
        job.schedule_removal()
    save_setting("broadcast_active", "0")


async def stop_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat admin to'xtata oladi.")
        return

    if not has_broadcast_job(context):
        await update.message.reply_text("Hozir hech narsa ishlamayapti ⏹")
        return

    stop_broadcast_job(context)
    await update.message.reply_text("Yuborish to'xtatildi ⏹")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_allowed(user_id):
        await update.message.reply_text("Sizga kirish berilgan ✅")
    else:
        await update.message.reply_text("Sizga kirish berilmagan ❌")


async def allow_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat egasi ruxsat berishi mumkin.")
        return

    if not context.args:
        await update.message.reply_text("Foydalanish uchun: /allow 123456789 30 yoki /allow 123456789 1h")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return

    duration_days = 30.0
    if len(context.args) > 1:
        parsed = parse_access_duration(context.args[1])
        if parsed is None:
            await update.message.reply_text("Muddati noto'g'ri. Masalan: 30, 2d, 1h, 30m")
            return
        duration_days = parsed

    if duration_days <= 0:
        await update.message.reply_text("Muddati 0 dan katta bo'lishi kerak.")
        return

    expires_at = add_allowed_user(user_id, duration_days)
    if expires_at is None:
        await update.message.reply_text(f"{user_id} foydalanuvchisiga ruxsat berildi ✅")
        return

    label = "kun"
    if duration_days < 1:
        hours = duration_days * 24
        label = "soat"
        if hours < 1:
            label = "daqiqa"
    await update.message.reply_text(
        f"{user_id} foydalanuvchisiga {duration_days:.2f} {label}lik ruxsat berildi ✅\n"
        f"Muddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha"
    )


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat egasi ruxsatni olib tashlashi mumkin.")
        return

    if not context.args:
        await update.message.reply_text("Foydalanish uchun: /remove 123456789")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return

    remove_allowed_user(user_id)
    await update.message.reply_text(f"{user_id} foydalanuvchisining ruxsati olib tashlandi ✅")


async def list_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Faqat egasi ko'rishi mumkin.")
        return

    users = sorted(load_allowed_users())
    if not users:
        await update.message.reply_text("Hech kimga ruxsat berilmagan.")
        return

    lines = []
    for uid in users:
        if uid == OWNER_ID:
            lines.append(f"{uid} - admin")
        else:
            expiry = get_premium_expiry(uid)
            if expiry is None:
                expires_at = "cheksiz"
            else:
                expires_at = expiry.strftime("%d.%m.%Y %H:%M")
            lines.append(f"{uid} - Pro: {expires_at}")
    await update.message.reply_text("Ruxsatli foydalanuvchilar:\n" + "\n".join(lines))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query = update.callback_query
    data = query.data
    await query.answer()

    is_purchase_action = data.startswith(("buy_balance_", "buy_admin_"))
    public_actions = {"premium", "balance", "back_main", "start_broadcast", "stop_broadcast", "user_stats"}
    public_actions.update(f"plan_{plan_id}" for plan_id in PRO_PLAN_DAYS)
    if (
        data not in public_actions
        and not is_purchase_action
        and not is_allowed(user_id)
    ):
        await query.answer("Kirish taqiqlangan", show_alert=True)
        return

    responses = {
        "profile": (
            "👥 Profillar\n\n"
            
        ),
        "message": "📣 Xabar tayyorlandi va yuborishga tayyor.",
        "groups": "👥 Guruhlar: faol guruhlar ro'yxati\n\nUshbu bo'limda guruhlar boshqariladi.",
        "interval": "⏱ Interval: 2 daqiqa\n\nXabarlar muntazam yuboriladi.",
        "referral": "📨 Referral dastur: do'stlaringizni taklif qiling va bonus oling.",
        "admin": "⚙️ Admin bo'limi: faqat egasi kirishi mumkin.",
        "back_main": "Asosiy menyu ochildi.",
        "menu": "Menyu ochildi.",
        "premium": "💎 Pro bo'limi ochildi. Tarifni tanlang:",
        "plan_1m": f"💳 1 oy Pro tanlandi. Narx: {format_price(get_premium_price(1))}\n\nAdminga ariza yuborildi. Admin tasdiqlagandan keyin Pro faollashadi.",
        "plan_3m": f"💎 3 oy Pro tanlandi. Narx: {format_price(get_premium_price(3))}\n\nAdminga ariza yuborildi. Admin tasdiqlagandan keyin Pro faollashadi.",
        "plan_12m": f"🏆 12 oy Pro tanlandi. Narx: {format_price(get_premium_price(12))}\n\nAdminga ariza yuborildi. Admin tasdiqlagandan keyin Pro faollashadi.",
    }

    if data == "profile":
        name, phone = load_user_profile(user_id)
        if not name:
            name = update.effective_user.full_name
            save_user_profile(user_id, name=name)
        display_name = name or "Noma'lum"
        await query.edit_message_text(
            "👤 Profil\n\n"
            f"Ism: {display_name}\n"
            f"Telefon: {phone or '+998'}\n"
            f"Pro qolgan muddat: {format_pro_remaining(user_id)}\n\n"
            "Sizning profil ma'lumotlaringiz ko'rsatilmoqda."
        )
        return

    if data == "premium":
        await query.edit_message_text(
            f"💎 Pro paketlar\n\n{premium_prices_text()}",
            reply_markup=build_premium_menu(),
        )
        return

    if data == "balance":
        await query.edit_message_text(
            f"💰 Balansingiz: {format_price(get_user_balance(user_id))} so'm",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Balansni to'ldirish uchun admin", url="https://t.me/kbrsz_aka")]]
            ),
        )
        return

    if data == "interval":
        selected = get_user_broadcast_interval(user_id)
        await query.edit_message_text(
            f"Xabar yuborish intervali: {selected} daqiqa. Tanlang:",
            reply_markup=build_broadcast_interval_menu(selected),
        )
        return

    if data.startswith("set_broadcast_interval_"):
        try:
            minutes = int(data.removeprefix("set_broadcast_interval_"))
        except ValueError:
            await query.edit_message_text("Interval tanlovi noto'g'ri.")
            return
        if minutes not in BROADCAST_INTERVAL_MINUTES:
            await query.edit_message_text("Bu interval qo'llab-quvvatlanmaydi.")
            return
        set_user_broadcast_interval(user_id, minutes)
        await query.edit_message_text(
            f"Interval saqlandi: {minutes} daqiqa ✅",
            reply_markup=build_broadcast_interval_menu(minutes),
        )
        return

    if data == "interval":
        selected_minutes = get_user_broadcast_interval(user_id)
        await query.edit_message_text(
            f"Har {selected_minutes} daqiqada yuboriladi. Intervalni tanlang:",
            reply_markup=build_broadcast_interval_menu(selected_minutes),
        )
        return

    if data.startswith("set_broadcast_interval_"):
        try:
            minutes = int(data.removeprefix("set_broadcast_interval_"))
        except ValueError:
            await query.edit_message_text("Interval noto'g'ri tanlangan.")
            return
        if minutes not in BROADCAST_INTERVAL_MINUTES:
            await query.edit_message_text("Bu interval qo'llab-quvvatlanmaydi.")
            return
        set_user_broadcast_interval(user_id, minutes)
        await query.edit_message_text(
            f"Interval saqlandi: har {minutes} daqiqada ✅\n"
            "Endi «Boshlash» tugmasini bosib xabar yuboring.",
            reply_markup=build_broadcast_interval_menu(minutes),
        )
        return

    if data == "referral":
        try:
            bot_username = (await context.bot.get_me()).username
        except Exception:
            logging.exception("Could not get bot username for referral link")
            bot_username = None
        if not bot_username:
            await query.edit_message_text("Referral havolasi hozircha mavjud emas.")
            return
        total, rewarded = get_referral_stats(user_id)
        bonus = get_referral_bonus_amount()
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await query.edit_message_text(
            f"📨 Shaxsiy referral havolangiz:\n{referral_link}\n\n"
            f"Taklif qilinganlar: {total}\n"
            f"Pro olganlar: {rewarded}\n"
            f"Har bir Pro xaridi uchun bonus: {format_price(bonus)} so'm balansga qo'shiladi."
        )
        return

    if data == "start_broadcast":
        if not is_allowed(user_id):
            await query.answer("Kirish taqiqlangan", show_alert=True)
            return
        if not load_setting("group_chat_id", "").strip():
            await query.edit_message_text("Guruh ID belgilanmagan. Admin /setgroup bilan sozlang.")
            return
        context.user_data["pending_broadcast_message"] = True
        context.user_data["pending_group_message"] = False
        await query.edit_message_text(
            f"Guruhga har {get_user_broadcast_interval(user_id)} daqiqada yuboriladigan xabarni yuboring."
        )
        return

    if data == "stop_broadcast":
        if not is_allowed(user_id):
            await query.answer("Kirish taqiqlangan", show_alert=True)
            return
        if not has_broadcast_job(context, user_id):
            await query.edit_message_text("Hozir hech narsa ishlamayapti ⏹")
            return
        stop_user_broadcast_job(context, user_id)
        await query.edit_message_text("Avtomatik xabar yuborish to'xtatildi ⏹")
        return

    if data == "message":
        context.user_data["pending_group_message"] = True
        context.user_data["pending_broadcast_message"] = False
        await query.edit_message_text("Xabarni yozing. U guruhga yuboriladi ✅")
        return

    if data.startswith("plan_") and data.removeprefix("plan_") in PRO_PLAN_DAYS:
        plan_id = data.removeprefix("plan_")
        await query.edit_message_text(
            f"{PRO_PLAN_NAMES[plan_id]} Pro paketi: {format_price(get_pro_price(plan_id))} so'm\n"
            f"Balansingiz: {format_price(get_user_balance(user_id))} so'm\n\n"
            "Xarid usulini tanlang:",
            reply_markup=build_pro_purchase_menu(plan_id, get_user_balance(user_id)),
        )
        return

    if data.startswith(("buy_balance_", "buy_admin_")):
        try:
            mode, plan_id = data.removeprefix("buy_").split("_", 1)
        except ValueError:
            await query.edit_message_text("Pro paketi noto'g'ri tanlangan.")
            return
        if plan_id not in PRO_PLAN_DAYS:
            await query.edit_message_text("Noma'lum Pro paketi.")
            return

        if mode == "balance":
            purchased, expires_at, balance, price = purchase_pro_with_balance(user_id, plan_id)
            if not purchased:
                await query.edit_message_text(
                    f"Balans yetarli emas. Narx: {format_price(price)} so'm; "
                    f"balansingiz: {format_price(balance)} so'm.\n"
                    "Balansni admin orqali to'ldiring yoki admin orqali xarid qiling.",
                    reply_markup=build_pro_purchase_menu(plan_id, balance),
                )
                return
            await notify_referrer_of_pro_activation(context, user_id)
            await query.edit_message_text(
                f"{PRO_PLAN_NAMES[plan_id]} Pro balansdan sotib olindi ✅\n"
                f"Pro muddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha\n"
                f"Qolgan balans: {format_price(balance)} so'm"
            )
            return

        if mode != "admin":
            await query.edit_message_text("Xarid usuli noto'g'ri tanlangan.")
            return

        package_name = PRO_PLAN_NAMES[plan_id]
        name, phone = load_user_profile(user_id)
        username = update.effective_user.username
        if not phone:
            phone = "+998"

        save_premium_request(user_id, plan_id, name=name, phone=phone)
        admin_text = (
            f"Yangi Pro ariza:\n"
            f"User ID: {user_id}\n"
            f"Username: @{username if username else 'mavjud emas'}\n"
            f"Ism: {name or 'Noma\'lum'}\n"
            f"Telefon: {phone}\n"
            f"Paket: {package_name}\n"
            f"Status: kutilmoqda\n"
            f"Narx: {format_price(get_pro_price(plan_id))}"
        )
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=admin_text,
                reply_markup=build_premium_request_keyboard(user_id),
            )
        except Exception:
            pass
        await query.edit_message_text(
            f"{PRO_PLAN_NAMES[plan_id]} Pro paketi uchun ariza yuborildi.\n\n"
            "Pro so'rovi adminga yuborildi. Admin tasdiqlagach, Pro avtomatik faollashadi.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📞 Admin bilan bog'lanish", url="https://t.me/kbrsz_aka")]]
            ),
        )
        return

    if data.startswith(("premium_approve_", "premium_reject_")):
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin Pro so'rovini ko'rib chiqishi mumkin.")
            return
        action, requested_user_id = data.removeprefix("premium_").rsplit("_", 1)
        try:
            requested_user_id = int(requested_user_id)
        except ValueError:
            await query.edit_message_text("So'rov ma'lumotida xatolik bor.")
            return

        if action == "approve":
            approved, expires_at = approve_premium_request(requested_user_id, OWNER_ID)
            if not approved:
                await query.edit_message_text("Bu Pro so'rovi topilmadi yoki oldin ko'rib chiqilgan.")
                return
            await notify_referrer_of_pro_activation(context, requested_user_id)
            result_text = (
                f"{requested_user_id} foydalanuvchining Pro so'rovi qabul qilindi ✅\n"
                f"Muddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha"
            )
            user_text = f"Pro so'rovingiz qabul qilindi ✅\nMuddati: {expires_at.strftime('%d.%m.%Y %H:%M')} gacha"
        else:
            if not reject_premium_request(requested_user_id):
                await query.edit_message_text("Bu Pro so'rovi topilmadi yoki oldin ko'rib chiqilgan.")
                return
            result_text = f"{requested_user_id} foydalanuvchining Pro so'rovi rad etildi ❌"
            user_text = "Pro so'rovingiz rad etildi ❌"

        await query.edit_message_text(result_text)
        try:
            await context.bot.send_message(chat_id=requested_user_id, text=user_text)
        except Exception:
            logging.info("Premium decision notification could not be sent to user %s", requested_user_id)
        return

    if data == "back_main":
        await query.edit_message_text(
            "<b>Auto xabar bot</b>\n\n"
            "Bu bot bilan quyidagilarni qilsangiz bo'ladi:\n"
            "• avtomatik xabarlar\n"
            "• profil va statistika\n"
            "• guruhlar va intervallar\n"
            "• referral va Pro funktsiyalar\n\n"
            f"{premium_prices_text()}",
            reply_markup=build_main_menu(is_admin=user_id == OWNER_ID),
            parse_mode="HTML",
        )
        return

    if data == "admin":
        is_owner = user_id == OWNER_ID
        if not is_owner:
            await query.edit_message_text("⚠️ Faqat admin ko'rishi mumkin.")
            return
        await query.edit_message_text(
            "<b>⚙️ Admin panel</b>\n\n"
            "Buyruqlar:\n"
            "/status\n"
            "/premium_requests\n"
            "/approve_premium 123456 1\n"
            "/reject_premium 123456\n"
            "/grant_premium 123456 3\n"
            "/revoke_premium 123456",
            reply_markup=build_admin_menu(),
            parse_mode="HTML",
        )
        return

    if data == "user_stats":
        last_day, last_week = get_group_message_stats(sender_user_id=user_id)
        await query.edit_message_text(
            "📊 Statistika\n\n"
            f"Siz yuborgan xabarlar (oxirgi 1 kun): {last_day} ta\n"
            f"Siz yuborgan xabarlar (oxirgi 1 hafta): {last_week} ta",
            reply_markup=build_main_menu(is_admin=user_id == OWNER_ID),
        )
        return

    if data == "stats":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Guruh statistikasini faqat admin ko'rishi mumkin.")
            return
        last_day, last_week = get_group_message_stats()
        await query.edit_message_text(
            "📊 Guruh statistikasi\n\n"
            f"Oxirgi 1 kunda yuborilgan: {last_day} ta xabar\n"
            f"Oxirgi 1 haftada yuborilgan: {last_week} ta xabar",
            reply_markup=build_admin_menu() if user_id == OWNER_ID else build_main_menu(),
        )
        return

    if data == "admin_add_balance":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Balansni faqat admin o'zgartira oladi.")
            return
        context.user_data.pop("pending_balance_remove", None)
        context.user_data["pending_balance_add"] = True
        await query.edit_message_text(
            "Foydalanuvchi ID si yoki @username va summani yuboring. "
            "Masalan: 123456789 50000 yoki @username 50000"
        )
        return

    if data == "admin_remove_balance":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Balansni faqat admin o'zgartira oladi.")
            return
        context.user_data.pop("pending_balance_add", None)
        context.user_data["pending_balance_remove"] = True
        await query.edit_message_text(
            "Foydalanuvchi ID si yoki @username va summani yuboring. "
            "Masalan: 123456789 50000 yoki @username 50000"
        )
        return

    if data == "admin_channel":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin kanal obunasini sozlay oladi.")
            return
        channel = load_setting("required_channel", "")
        channel_link = load_setting("required_channel_link", "")
        group = load_setting("required_group", "")
        group_link = load_setting("required_group_link", "")
        status = (
            f"Kanal: {channel or 'sozlanmagan'}\nKanal havolasi: {channel_link or '-'}\n\n"
            f"Guruh: {group or 'sozlanmagan'}\nGuruh havolasi: {group_link or '-'}"
        )
        await query.edit_message_text(
            f"<b>📢 Majburiy obuna</b>\n\n{status}\n\n"
            "Bot a'zolikni tekshirishi uchun uni kanal va guruhga qo'shing.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Kanalni sozlash", callback_data="admin_channel_set")],
                    [InlineKeyboardButton("Guruhni sozlash", callback_data="admin_group_set")],
                    [InlineKeyboardButton("Kanalni o'chirish", callback_data="admin_channel_clear")],
                    [InlineKeyboardButton("Guruhni o'chirish", callback_data="admin_group_clear")],
                    [InlineKeyboardButton("⬅️ Admin paneli", callback_data="admin")],
                ]
            ),
            parse_mode="HTML",
        )
        return

    if data == "admin_referral_bonus":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin referral bonusini sozlay oladi.")
            return
        await query.edit_message_text(
            f"Referral bonusi: {format_price(get_referral_bonus_amount())} so'm\n\n"
            "Har bir taklif qilingan foydalanuvchi Pro olganda bir marta beriladi.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Bonusni o'zgartirish", callback_data="admin_referral_bonus_set")],
                    [InlineKeyboardButton("⬅️ Admin paneli", callback_data="admin")],
                ]
            ),
        )
        return

    if data == "admin_referral_bonus_set":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin referral bonusini o'zgartira oladi.")
            return
        context.user_data["pending_referral_bonus"] = True
        await query.edit_message_text(
            "Yangi referral bonusini so'mda yuboring. 0 bonusni o'chiradi."
        )
        return

    if data == "admin_channel_set":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin kanal sozlay oladi.")
            return
        context.user_data["pending_required_channel"] = True
        await query.edit_message_text(
            "Ommaviy kanal uchun @username yuboring.\n"
            "Yopiq kanal uchun: -100... https://t.me/+taklif_havolasi"
        )
        return

    if data == "admin_channel_clear":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin kanal sozlamasini o'chira oladi.")
            return
        save_setting("required_channel", "")
        save_setting("required_channel_link", "")
        context.user_data.pop("pending_required_channel", None)
        await query.edit_message_text(
            "Majburiy kanal o'chirildi ✅",
            reply_markup=build_admin_menu(),
        )
        return

    if data == "admin_group_set":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin guruh sozlay oladi.")
            return
        context.user_data["pending_required_group"] = True
        await query.edit_message_text(
            "Ochiq guruh uchun @username yuboring.\n"
            "Yopiq guruh uchun: -100... https://t.me/+taklif_havolasi"
        )
        return

    if data == "admin_group_clear":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin guruh sozlamasini o'chira oladi.")
            return
        save_setting("required_group", "")
        save_setting("required_group_link", "")
        context.user_data.pop("pending_required_group", None)
        await query.edit_message_text(
            "Majburiy guruh o'chirildi ✅",
            reply_markup=build_admin_menu(),
        )
        return

    if data == "admin_premium_users":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin Pro foydalanuvchilarni ko'ra oladi.")
            return
        rows = list_active_premium_users()
        if not rows:
            await query.edit_message_text(
                "Hozir faol Pro foydalanuvchilar yo'q.",
                reply_markup=build_admin_menu(),
            )
            return

        await query.edit_message_text(
            f"💎 Faol Pro foydalanuvchilar: {len(rows)} ta",
            reply_markup=build_admin_menu(),
        )
        for user_id, username, name, phone, premium_until in rows:
            username_text = f"@{username}" if username else "mavjud emas"
            section = (
                f"💎 Pro foydalanuvchi\n"
                f"🆔 ID: {user_id}\n"
                f"Username: {username_text}\n"
                f"Ism: {name or 'Noma\'lum'}\n"
                f"Telefon: {phone or 'mavjud emas'}\n"
                f"Pro muddati: {premium_until}"
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=section,
                reply_markup=build_premium_user_actions([user_id]),
            )
            if user_id != rows[-1][0]:
                await asyncio.sleep(1)
        return

    if data.startswith("premium_revoke_done_"):
        return

    if data.startswith("premium_revoke_"):
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin Pro ruxsatini olib tashlay oladi.")
            return
        try:
            revoked_user_id = int(data.removeprefix("premium_revoke_"))
        except ValueError:
            await query.edit_message_text("Foydalanuvchi ID noto'g'ri.")
            return

        revoke_premium_access(revoked_user_id)
        buttons = [
            [
                InlineKeyboardButton(
                    "✅ Ruxsat olib tashlandi" if button.callback_data == data else button.text,
                    callback_data=f"premium_revoke_done_{revoked_user_id}"
                    if button.callback_data == data
                    else button.callback_data,
                    url=button.url if button.callback_data != data else None,
                )
                for button in row
            ]
            for row in query.message.reply_markup.inline_keyboard
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        try:
            await context.bot.send_message(
                chat_id=revoked_user_id,
                text="Admin Pro ruxsatingizni olib tashladi. Botdan foydalanish uchun Pro paket sotib oling.",
            )
        except Exception:
            logging.info("Premium revocation notification could not be sent to user %s", revoked_user_id)
        return

    if data == "admin_prices":
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin ko'rishi mumkin.")
            return
        context.user_data.pop("pending_pro_price_plan", None)
        await query.edit_message_text(
            f"<b>💰 Pro narxlari</b>\n\n{premium_prices_text()}\n\n"
            "O'zgartirmoqchi bo'lgan paketni tanlang:",
            reply_markup=build_admin_prices_menu(),
            parse_mode="HTML",
        )
        return

    if data.startswith("admin_price_"):
        if user_id != OWNER_ID:
            await query.edit_message_text("⚠️ Faqat admin ko'rishi mumkin.")
            return
        plan_id = data.removeprefix("admin_price_")
        if plan_id not in PRO_PLAN_DAYS:
            await query.edit_message_text("Paket tanlovida xatolik yuz berdi.")
            return
        context.user_data["pending_pro_price_plan"] = plan_id
        await query.edit_message_text(
            f"{PRO_PLAN_NAMES[plan_id]} uchun yangi narxni raqam bilan yuboring.\n"
            f"Joriy narx: {format_price(get_pro_price(plan_id))}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Bekor qilish", callback_data="admin_prices")]]
            ),
        )
        return

    await query.edit_message_text(responses.get(data, "Tanlov qabul qilindi."))


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = update.message.contact
    if contact is None:
        return

    user_id = update.effective_user.id
    if contact.user_id != user_id:
        await update.message.reply_text(
            "Iltimos, ro'yxatdan o'tish uchun o'zingizning telefon raqamingizni yuboring.",
            reply_markup=build_phone_request_keyboard(),
        )
        return

    phone = contact.phone_number
    save_user_profile(
        user_id,
        name=update.effective_user.full_name,
        phone=phone,
        username=update.effective_user.username,
    )
    save_setting("broadcast_phone", phone)

    await update.message.reply_text(
        f"Telefon raqamingiz saqlandi ✅\n\n{phone}\n\nEndi botdan foydalanishingiz mumkin.",
        reply_markup=ReplyKeyboardRemove(),
    )

    await start(update, context)


async def text_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text or ""

    if context.user_data.get("pending_required_channel"):
        if user_id != OWNER_ID:
            context.user_data.pop("pending_required_channel", None)
            await update.message.reply_text("Faqat admin kanal sozlay oladi.")
            return
        parsed_channel = parse_required_channel(text)
        if parsed_channel is None:
            await update.message.reply_text(
                "Kanal formatini tekshiring. Ommaviy: @kanalusername; "
                "yopiq: -100... https://t.me/+taklif_havolasi"
            )
            return
        channel, channel_link = parsed_channel
        save_setting("required_channel", channel)
        save_setting("required_channel_link", channel_link)
        context.user_data.pop("pending_required_channel", None)
        await update.message.reply_text(
            f"Majburiy kanal sozlandi: {channel} ✅",
            reply_markup=build_admin_menu(),
        )
        return

    if context.user_data.get("pending_required_group"):
        if user_id != OWNER_ID:
            context.user_data.pop("pending_required_group", None)
            await update.message.reply_text("Faqat admin guruh sozlay oladi.")
            return
        parsed_group = parse_required_channel(text)
        if parsed_group is None:
            await update.message.reply_text(
                "Guruh formatini tekshiring. Ochiq: @username; "
                "yopiq: -100... https://t.me/+taklif_havolasi"
            )
            return
        group, group_link = parsed_group
        save_setting("required_group", group)
        save_setting("required_group_link", group_link)
        context.user_data.pop("pending_required_group", None)
        await update.message.reply_text(
            f"Majburiy guruh sozlandi: {group} ✅\n"
            "Bot a'zolikni tekshirishi uchun botni guruhga admin qilib qo'shing.",
            reply_markup=build_admin_menu(),
        )
        return

    if context.user_data.get("pending_balance_add"):
        if user_id != OWNER_ID:
            context.user_data.pop("pending_balance_add", None)
            await update.message.reply_text("Balansni faqat admin o'zgartira oladi.")
            return
        parsed_balance = parse_balance_target(text)
        if parsed_balance is None:
            await update.message.reply_text(
                "Format noto'g'ri. ID yoki @username va musbat summani yuboring, "
                "masalan: 123456789 50000 yoki @username 50000"
            )
            return
        target, amount = parsed_balance
        target_user_id = resolve_balance_user_id(target)
        if target_user_id is None:
            await update.message.reply_text(
                "Foydalanuvchi topilmadi. Username botda /start yuborgan bo'lishi kerak."
            )
            return
        new_balance = add_user_balance(target_user_id, amount)
        context.user_data.pop("pending_balance_add", None)
        await update.message.reply_text(
            f"{target_user_id} foydalanuvchining balansiga {format_price(amount)} so'm qo'shildi. "
            f"Yangi balans: {format_price(new_balance)} so'm ✅",
            reply_markup=build_admin_menu(),
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"Balansingizga {format_price(amount)} so'm qo'shildi. Joriy balans: {format_price(new_balance)} so'm.",
            )
        except Exception:
            logging.info("Balance update notification could not be sent to user %s", target_user_id)
        return

    if context.user_data.get("pending_balance_remove"):
        if user_id != OWNER_ID:
            context.user_data.pop("pending_balance_remove", None)
            await update.message.reply_text("Balansni faqat admin o'zgartira oladi.")
            return
        parsed_removal = parse_balance_target(text)
        if parsed_removal is None:
            await update.message.reply_text(
                "Format noto'g'ri. ID yoki @username va musbat summani yuboring, "
                "masalan: 123456789 50000 yoki @username 50000"
            )
            return
        target, amount = parsed_removal
        target_user_id = resolve_balance_user_id(target)
        if target_user_id is None:
            await update.message.reply_text(
                "Foydalanuvchi topilmadi. Username botda ro'yxatdan o'tgan bo'lishi kerak."
            )
            return
        new_balance = remove_user_balance(target_user_id, amount)
        if new_balance is None:
            current_balance = get_user_balance(target_user_id)
            await update.message.reply_text(
                f"Balans yetarli emas. Joriy balans: {format_price(current_balance)} so'm."
            )
            return
        context.user_data.pop("pending_balance_remove", None)
        await update.message.reply_text(
            f"{target_user_id} foydalanuvchining balansidan {format_price(amount)} so'm olindi. "
            f"Yangi balans: {format_price(new_balance)} so'm ✅",
            reply_markup=build_admin_menu(),
        )
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"Balansingizdan {format_price(amount)} so'm olindi. Joriy balans: {format_price(new_balance)} so'm.",
            )
        except Exception:
            logging.info("Balance removal notification could not be sent to user %s", target_user_id)
        return

    if context.user_data.get("pending_referral_bonus"):
        if user_id != OWNER_ID:
            context.user_data.pop("pending_referral_bonus", None)
            await update.message.reply_text("Referral bonusini faqat admin o'zgartira oladi.")
            return
        amount_text = text.strip().replace(".", "").replace(" ", "")
        if not amount_text.isdigit():
            await update.message.reply_text("Bonusni 0 yoki musbat so'm miqdorida yuboring.")
            return
        bonus = int(amount_text)
        save_setting("referral_bonus", str(bonus))
        context.user_data.pop("pending_referral_bonus", None)
        await update.message.reply_text(
            f"Referral bonusi {format_price(bonus)} so'm qilib saqlandi ✅",
            reply_markup=build_admin_menu(),
        )
        return

    pending_plan = context.user_data.get("pending_pro_price_plan")
    if pending_plan is not None:
        if user_id != OWNER_ID:
            context.user_data.pop("pending_pro_price_plan", None)
            await update.message.reply_text("Faqat admin Pro narxini o'zgartira oladi.")
            return
        price = parse_premium_price(text)
        if price is None:
            await update.message.reply_text("Narxni musbat raqam bilan yuboring. Masalan: 150000 yoki 150.000")
            return
        set_pro_price(pending_plan, price)
        context.user_data.pop("pending_pro_price_plan", None)
        await update.message.reply_text(
            f"{PRO_PLAN_NAMES[pending_plan]} Pro narxi {format_price(price)} qilib saqlandi ✅",
            reply_markup=build_admin_prices_menu(),
        )
        return

    _, phone = load_user_profile(user_id)
    if not has_registered_phone(phone):
        await update.message.reply_text(
            "Botdan foydalanish uchun telefon raqamingizni yuboring.",
            reply_markup=build_phone_request_keyboard(),
        )
        return

    if context.user_data.get("pending_group_message"):
        group_id = load_setting("group_chat_id", "")
        if group_id:
            try:
                await context.bot.send_message(chat_id=int(group_id), text=text)
                record_group_message_sent(user_id)
                await update.message.reply_text("Xabar guruhga yuborildi ✅")
            except Exception:
                await update.message.reply_text("Guruhga xabar yuborilmadi. Admin /setgroup ni sozlang.")
        else:
            await update.message.reply_text("Guruh ID belgilanmagan. Admin /setgroup bilan sozlang.")
        context.user_data["pending_group_message"] = False
        return

    if context.user_data.get("pending_broadcast_message"):
        if not is_allowed(user_id):
            context.user_data["pending_broadcast_message"] = False
            await update.message.reply_text(
                "Avtomatik xabar yuborish uchun faol Pro obuna kerak."
            )
            return
        clean_text = text.strip()
        if not clean_text:
            await update.message.reply_text("Bo'sh xabar yuborib bo'lmaydi. Xabarni qaytadan yuboring.")
            return
        if not load_setting("group_chat_id", "").strip():
            context.user_data["pending_broadcast_message"] = False
            await update.message.reply_text("Guruh ID belgilanmagan. Admin /setgroup bilan sozlang.")
            return

        context.user_data["pending_broadcast_message"] = False
        interval_minutes = get_user_broadcast_interval(user_id)
        if not start_user_broadcast_job(context, user_id, clean_text, interval_minutes):
            await update.message.reply_text("Avtomatik xabar yuborish ishga tushmadi. Qayta urinib ko'ring.")
            return
        await update.message.reply_text(f"Xabar guruhga har {interval_minutes} daqiqada yuboriladi ✅")
        return

    if not is_allowed(user_id):
        await update.message.reply_text(
            "Kechirasiz, sizga kirish taqiqlangan.\n"
            "Botdan foydalanish uchun avval Pro sotib oling.\n\n"
            "💎 Tariflar:\n"
            f"{premium_prices_text()}\n\n"
            "Admin: @kbrsz_aka"
        )
        return

    normalized = text.strip().lower()
    if normalized in BROADCAST_START_WORDS:
        message = load_setting("broadcast_message", "").strip()
        if not message:
            await update.message.reply_text("Avval xabar yozing, keyin avtomatik yuborishni ishga tushirish mumkin.")
            return
        start_broadcast_job(context, message)
        await update.message.reply_text("Avtomatik yuborish ishga tushdi. Foydalanuvchi 'toxtat' deb yozmaguncha har 2 daqiqada xabar yuboriladi.")
        return

    if normalized in BROADCAST_STOP_WORDS:
        stop_broadcast_job(context)
        await update.message.reply_text("Avtomatik xabar yuborish to'xtatildi ⏹")
        return

    await update.message.reply_text(f"Siz yozdingiz: {text}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Telegram xato yuz berdi", exc_info=context.error)
    if update is not None:
        try:
            if hasattr(update, "effective_chat") and update.effective_chat is not None:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Botda vaqtinchalik xatolik yuz berdi. Iltimos, bir soniya ichida qayta urinib ko'ring.",
                )
        except Exception:
            pass


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Bot tokeni topilmadi. TELEGRAM_BOT_TOKEN yoki BOT_TOKEN muhit o'zgaruvchisini yoki .env faylini sozlang. "
            "Masalan: TELEGRAM_BOT_TOKEN=123456:ABCDEF"
        )

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(MessageHandler(filters.ALL, required_channel_gate), group=-2)
    app.add_handler(CallbackQueryHandler(required_channel_gate), group=-2)
    app.add_handler(MessageHandler(filters.ALL, registration_gate), group=-1)
    app.add_handler(CallbackQueryHandler(registration_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("osha", start_broadcast))
    app.add_handler(CommandHandler("toxtat", stop_broadcast))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler(["premium", "pro"], premium_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler(["premium_requests", "pro_requests"], premium_requests_command))
    app.add_handler(CommandHandler(["approve_premium", "approve_pro"], approve_premium_command))
    app.add_handler(CommandHandler(["reject_premium", "reject_pro"], reject_premium_command))
    app.add_handler(CommandHandler(["grant_premium", "grant_pro"], grant_premium_command))
    app.add_handler(CommandHandler(["revoke_premium", "revoke_pro"], revoke_premium_command))
    app.add_handler(CommandHandler("setgroup", set_group))
    app.add_handler(CommandHandler("setname", set_name))
    app.add_handler(CommandHandler("setphone", set_phone))
    app.add_handler(CommandHandler("setmessage", set_message))
    app.add_handler(CommandHandler("startbroadcast", start_broadcast))
    app.add_handler(CommandHandler("stopbroadcast", stop_broadcast))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_echo))

    resume_broadcast_job(app)
    print("Bot ishlayapti...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()