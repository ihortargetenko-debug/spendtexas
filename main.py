import os, re, datetime as dt, threading
from decimal import Decimal
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, text

from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# ---------- env ----------
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0"))
POST_CHAT_ID   = int(os.getenv("POST_CHAT_ID", "0")) or int(os.getenv("SOURCE_CHAT_ID", "0"))
TZ = os.getenv("TZ", "Europe/Bucharest")
PORT = int(os.environ.get("PORT", 8000))   # Replit will expose PORT

if not BOT_TOKEN or not SOURCE_CHAT_ID or not POST_CHAT_ID:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN, SOURCE_CHAT_ID, POST_CHAT_ID via Replit Secrets.")

# ---------- clusters & parsing ----------
CLUSTERS = ("TEXAS", "SKY", "ALX")
AMOUNT_RE = re.compile(r"(?:(?:USD|\$)\s*)?([\d\s.,]+)\s*(?:USD|\$)?", re.I)

engine = create_engine("sqlite:///daily_spend.db", future=True)
with engine.begin() as conn:
    conn.exec_driver_sql("""CREATE TABLE IF NOT EXISTS spends(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    message_id INTEGER,
    ymd TEXT,
    cluster TEXT,
    amount REAL
);
""")

def _norm_amount(txt: str) -> Decimal:
    s = txt.replace("\u202f", "").replace(" ", "")
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    if s.count(",") > 1 and s.count(".") == 0:
        s = s.replace(",", "")
    return Decimal(s)

def extract_cluster(text: str) -> str | None:
    up = text.upper()
    for c in CLUSTERS:
        if c in up:
            return c
    return None

# ---------- telegram handlers ----------
async def ingest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    cluster = extract_cluster(text)
    if not cluster:
        return

    matches = AMOUNT_RE.findall(text)
    if not matches:
        return
    amount = max((_norm_amount(m) for m in matches), default=Decimal(0))
    if amount <= 0:
        return

    ymd = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM spends WHERE chat_id=:c AND message_id=:m"),
            {"c": msg.chat.id, "m": msg.message_id}
        ).fetchone()
        if exists:
            return
        conn.execute(text("""            INSERT INTO spends(chat_id, message_id, ymd, cluster, amount)
            VALUES(:chat,:mid,:ymd,:cluster,:amt)
"""        ), {"chat": msg.chat.id, "mid": msg.message_id, "ymd": ymd, "cluster": cluster, "amt": float(amount)})

async def summarize_day(context: ContextTypes.DEFAULT_TYPE, ymd: str):
    with engine.begin() as conn:
        rows = conn.execute(text("""            SELECT cluster, ROUND(SUM(amount),2) AS total, COUNT(*) as n
            FROM spends WHERE ymd=:ymd
            GROUP BY cluster
            ORDER BY total DESC
"""        ), {"ymd": ymd}).fetchall()
        total = conn.execute(text("SELECT ROUND(SUM(amount),2) FROM spends WHERE ymd=:ymd"),
                             {"ymd": ymd}).scalar() or 0.0

    if rows:
        lines = [f"• {r.cluster}: ${r.total:,.2f} ({r.n} записей)" for r in rows]
        body = "\n".join(lines)
    else:
        body = "Данных нет."

    text_msg = f"📊 Сводка спенда за {ymd}\n{body}\n\nИТОГО: ${total:,.2f}"
    await context.bot.send_message(chat_id=POST_CHAT_ID, text=text_msg)

# команды
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ymd = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    await summarize_day(context, ymd)

async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    y = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    await summarize_day(context, y)

async def cmd_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ymd = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    await summarize_day(context, ymd)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот на Replit запущен. Слушаю чат и шлёю сводку в 23:59. Команды: /today /yesterday /summarize")

def schedule(app):
    sched = BackgroundScheduler(timezone=TZ)
    sched.add_job(lambda: app.create_task(
        summarize_day(app, dt.datetime.now().astimezone().strftime("%Y-%m-%d"))
    ), "cron", hour=23, minute=59, id="daily_summary")
    sched.start()

# ---------- mini web server for keep-alive ----------
from flask import Flask
flask_app = Flask(__name__)

@flask_app.get("/")
def root():
    return "OK"
@flask_app.get("/health")
def health():
    return "healthy"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ---------- bootstrap ----------
def main():
    # run Flask in a background thread
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, ingest))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday))
    app.add_handler(CommandHandler("summarize", cmd_summarize))

    schedule(app)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
