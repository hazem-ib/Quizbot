import os
import io
import re
import json
import hmac
import hashlib
import urllib.parse
import asyncio
import random
from typing import Optional, List
import pandas as pd
import psycopg2
from psycopg2 import pool
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll, MenuButtonWebApp
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9268644584:AAE8woNBoXzueYqpsT4x69efYcDgP1mPP9Y").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="Quiz Management Mini App")

db_pool: Optional[pool.ThreadedConnectionPool] = None
_db_initialized = False


def _clean_db_url(url: str) -> str:
    return url.replace("&channel_binding=require", "").replace("?channel_binding=require", "")


def get_pool() -> pool.ThreadedConnectionPool:
    global db_pool
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="متغير DATABASE_URL غير مضاف في إعدادات البيئة.")
    if db_pool is None:
        db_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=_clean_db_url(DATABASE_URL),
            sslmode="require",
            connect_timeout=10,
        )
    return db_pool


class PooledConn:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        p = get_pool()
        self.conn = p.getconn()
        try:
            if self.conn.closed:
                p.putconn(self.conn, close=True)
                self.conn = p.getconn()
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            try:
                p.putconn(self.conn, close=True)
            except Exception:
                pass
            self.conn = p.getconn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            try:
                if exc_type is not None:
                    self.conn.rollback()
                get_pool().putconn(self.conn)
            except Exception:
                pass


def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, name TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS banks (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
            c.execute("""CREATE TABLE IF NOT EXISTS questions (
                            id SERIAL PRIMARY KEY,
                            bank_id INTEGER REFERENCES banks(id) ON DELETE CASCADE,
                            question TEXT,
                            opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT,
                            correct_idx INTEGER,
                            explanation TEXT DEFAULT '')""")
            c.execute("""CREATE TABLE IF NOT EXISTS communities (
                            id SERIAL PRIMARY KEY,
                            title TEXT,
                            chat_id TEXT,
                            topic_id INTEGER DEFAULT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank_id ON questions(bank_id)")
        conn.commit()
    _db_initialized = True


@app.on_event("startup")
async def on_startup():
    try:
        init_db()
    except Exception:
        pass


def get_owner_id() -> Optional[int]:
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM config WHERE key = 'owner_id'")
            row = c.fetchone()
    return int(row[0]) if row else None


def set_owner_id(user_id: int, name: str = "Owner"):
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO config (key, value) VALUES ('owner_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(user_id),),
            )
            c.execute(
                "INSERT INTO admins (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, name),
            )
        conn.commit()


def is_admin(user_id: int) -> bool:
    owner = get_owner_id()
    if owner is None:
        return False
    if user_id == owner:
        return True
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
            res = c.fetchone()
    return res is not None


def verify_telegram_init_data(init_data: str) -> dict:
    init_db()
    if not init_data or not init_data.strip():
        raise HTTPException(status_code=403, detail="⛔ غير مصرح: يجب فتح التطبيق من داخل تيليجرام.")

    parsed = dict(urllib.parse.parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        raise HTTPException(status_code=403, detail="⛔ توقيع تيليجرام مفقود.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if calculated_hash != hash_val:
        raise HTTPException(status_code=403, detail="⛔ فشل التحقق الأمني من هوية تيليجرام.")

    user_info = json.loads(parsed.get("user", "{}"))
    user_id = user_info.get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="⛔ تعذر قراءة معرف المستخدم.")

    if get_owner_id() is None:
        set_owner_id(user_id, user_info.get("first_name", "Owner"))

    if not is_admin(user_id):
        raise HTTPException(status_code=403, detail="⛔ هذا التطبيق مخصص للمالك والمشرفين فقط.")
    return user_info


def get_base_url(request: Request) -> str:
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"https://{host}".rstrip("/")


@app.get("/", response_class=HTMLResponse)
async def serve_home():
    html_path = os.path.join(os.path.dirname(__file__), "..", "public", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>App Ready! Open /api/setup_webhook</h1>"


class QuestionUpdate(BaseModel):
    question: str
    opt1: str
    opt2: str
    opt3: str
    opt4: str
    correct_idx: int
    explanation: Optional[str] = ""


class QuestionCreate(QuestionUpdate):
    bank_id: int


class MoveQuestionReq(BaseModel):
    target_bank_id: int


class BankRenameReq(BaseModel):
    name: str


class CommunityCreateReq(BaseModel):
    title: str
    chat_id: str
    topic_id: Optional[int] = None


class BatchPublishReq(BaseModel):
    question_ids: List[int]
    target_type: str
    chat_id: Optional[str] = None
    target_name: Optional[str] = None
    topic_id: Optional[int] = None
    delay_sec: float = 0.0
    shuffle_options: bool = False
    open_period: Optional[int] = None


class AdminAddReq(BaseModel):
    user_id: int
    name: Optional[str] = "Admin"


def build_bot_app(webapp_url: str = ""):
    application = Application.builder().token(BOT_TOKEN).build()

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        init_db()
        user = update.effective_user
        if get_owner_id() is None:
            set_owner_id(user.id, user.first_name)

        if not is_admin(user.id):
            await update.message.reply_text("⛔ هذا البوت مخصص للمشرفين فقط.")
            return

        url = webapp_url or os.environ.get("RENDER_EXTERNAL_URL", "")
        keyboard = [[InlineKeyboardButton("🚀 فتح لوحة إدارة الاختبارات", web_app=WebAppInfo(url=url))]]
        await update.message.reply_text(
            f"👋 أهلاً بك يا **{user.first_name}**!\n\n"
            "• افتح لوحة التحكم لإدارة بنوك الأسئلة، القنوات والمجموعات، وجدولة الإرسال.\n"
            "• أرسل `/id` داخل أي مجموعة أو موضوع (Topic) أو حوّل رسالة من القناة لاستخراج المعرّف تلقائياً.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def get_chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        msg = update.effective_message
        topic_id = getattr(msg, "message_thread_id", None)
        text = f"📌 **بيانات الوجهة:**\n• **الاسم:** `{chat.title or chat.first_name}`\n• **Chat ID:** `{chat.id}`"
        if topic_id:
            text += f"\n• **Topic ID:** `{topic_id}`"
        await msg.reply_text(text, parse_mode="Markdown")

    async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.forward_origin:
            origin = msg.forward_origin
            if hasattr(origin, "chat") and origin.chat:
                await msg.reply_text(
                    f"✅ **تم استخراج بيانات القناة/المجموعة:**\n"
                    f"• **الاسم:** `{origin.chat.title}`\n"
                    f"• **Chat ID:** `{origin.chat.id}`",
                    parse_mode="Markdown",
                )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", get_chat_id_cmd))
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    return application


@app.get("/api/setup_webhook")
@app.get("/setup_webhook")
async def setup_webhook(request: Request):
    init_db()
    base_url = get_base_url(request)
    webhook_url = f"{base_url}/api/webhook"

    bot_app = build_bot_app(base_url)
    async with bot_app:
        await bot_app.bot.set_webhook(url=webhook_url)
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📱 إدارة الاختبارات", web_app=WebAppInfo(url=base_url))
        )
    return {
        "status": "Webhook & Menu Button configured successfully on Render!",
        "app_url": base_url,
        "webhook_url": webhook_url,
    }


@app.post("/api/webhook")
@app.post("/webhook")
async def telegram_webhook(request: Request):
    base_url = get_base_url(request)
    data = await request.json()
    bot_app = build_bot_app(base_url)
    async with bot_app:
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
    return {"ok": True}


@app.get("/api/me")
@app.get("/me")
async def get_me(x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    uid = user.get("id")
    owner = get_owner_id()
    return {"user": user, "is_owner": uid == owner, "owner_id": owner}


@app.get("/api/banks")
@app.get("/banks")
async def get_banks(x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT b.id, b.name, COUNT(q.id)::int
                FROM banks b
                LEFT JOIN questions q ON q.bank_id = b.id
                GROUP BY b.id, b.name
                ORDER BY b.id DESC
            """)
            rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]


@app.post("/api/banks")
@app.post("/banks")
async def create_bank(req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="يرجى إدخال اسم المجموعة.")
    with PooledConn() as conn:
        try:
            with conn.cursor() as c:
                c.execute("INSERT INTO banks (name) VALUES (%s) RETURNING id", (name,))
                bank_id = c.fetchone()[0]
            conn.commit()
            return {"id": bank_id, "name": name}
        except psycopg2.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=400, detail="توجد مجموعة مسجلة بهذا الاسم مسبقاً.")


@app.put("/api/banks/{bank_id}")
@app.put("/banks/{bank_id}")
async def rename_bank(bank_id: int, req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="الاسم الجديد لا يمكن أن يكون فارغاً.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE banks SET name = %s WHERE id = %s", (name, bank_id))
        conn.commit()
    return {"status": "ok"}


@app.delete("/api/banks/{bank_id}")
@app.delete("/banks/{bank_id}")
async def delete_bank(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM banks WHERE id = %s", (bank_id,))
        conn.commit()
    return {"status": "ok"}


def parse_correct_index(ans_raw_val, opts: list) -> Optional[int]:
    if pd.isna(ans_raw_val):
        return None
    s = str(ans_raw_val).strip()
    if not s or s.lower() == "nan":
        return None

    s_norm = " ".join(s.split()).lower()
    for i, opt in enumerate(opts):
        opt_norm = " ".join(str(opt).split()).lower()
        if s_norm == opt_norm and opt_norm != "-":
            return i

    num_map = {"1": 0, "2": 1, "3": 2, "4": 3, "1.0": 0, "2.0": 1, "3.0": 2, "4.0": 3}
    if s in num_map:
        return num_map[s]

    s_clean = s.upper().rstrip(".").rstrip(")").strip()
    letter_map = {"A": 0, "B": 1, "C": 2, "D": 3, "أ": 0, "ا": 0, "ب": 1, "ج": 2, "د": 3}
    if s_clean in letter_map:
        return letter_map[s_clean]

    return None


@app.post("/api/upload_csv")
@app.post("/upload_csv")
async def upload_csv(
    bank_name: str = Form(default=""),
    bank_id: Optional[int] = Form(default=None),
    file: UploadFile = File(...),
    x_telegram_init_data: str = Header(default=""),
):
    verify_telegram_init_data(x_telegram_init_data)
    content = await file.read()
    df = None
    for enc in ["utf-8-sig", "utf-8", "cp1256", "windows-1256", "latin1"]:
        try:
            df = pd.read_csv(io.BytesIO(content), header=None, encoding=enc, sep=None, engine="python")
            break
        except Exception:
            continue
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail="تعذر قراءة ملف CSV أو الملف فارغ.")

    if df.shape[1] < 6:
        raise HTTPException(status_code=400, detail=f"الملف يحتوي على {df.shape[1]} أعمدة فقط؛ المطلوب 6 أعمدة على الأقل.")

    first_opts = [str(df.iloc[0, i]).strip() if not pd.isna(df.iloc[0, i]) else "" for i in range(1, 5)]
    if parse_correct_index(df.iloc[0, 5], first_opts) is None:
        df = df.iloc[1:]

    rows_to_insert = []
    for idx, row in df.iterrows():
        if pd.isna(row[0]) or str(row[0]).strip() == "":
            continue
        q_text = str(row[0]).strip()
        opts = [
            str(row[i]).strip() if not pd.isna(row[i]) and str(row[i]).strip() != "" else "-"
            for i in range(1, 5)
        ]
        if all(o == "-" for o in opts):
            continue

        c_idx = parse_correct_index(row[5], opts)
        if c_idx is None:
            val_found = "" if pd.isna(row[5]) else str(row[5]).strip()
            raise HTTPException(
                status_code=400,
                detail=f"خطأ في الصف {idx + 1} (السؤال: {q_text[:30]}...): قيمة الإجابة الصحيحة غير صالحة ('{val_found}').",
            )

        expl = str(row[6]).strip() if df.shape[1] > 6 and not pd.isna(row[6]) else ""
        rows_to_insert.append((q_text, opts[0], opts[1], opts[2], opts[3], c_idx, expl))

    with PooledConn() as conn:
        try:
            with conn.cursor() as c:
                if bank_id is not None:
                    target_bank_id = bank_id
                else:
                    clean_bank = bank_name.strip()
                    if not clean_bank:
                        raise HTTPException(status_code=400, detail="يرجى إدخال اسم المجموعة.")
                    c.execute("INSERT INTO banks (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (clean_bank,))
                    c.execute("SELECT id FROM banks WHERE name = %s", (clean_bank,))
                    target_bank_id = c.fetchone()[0]

                for q_text, o1, o2, o3, o4, c_idx, expl in rows_to_insert:
                    c.execute(
                        """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (target_bank_id, q_text, o1, o2, o3, o4, c_idx, expl),
                    )
            conn.commit()
            return {"status": "ok", "added": len(rows_to_insert), "bank_id": target_bank_id}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=f"تعذر حفظ الأسئلة: {str(e)}")


@app.get("/api/banks/{bank_id}/questions")
@app.get("/banks/{bank_id}/questions")
async def get_questions(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC",
                (bank_id,),
            )
            rows = c.fetchall()
    return [
        {
            "id": r[0],
            "question": r[1],
            "opt1": r[2],
            "opt2": r[3],
            "opt3": r[4],
            "opt4": r[5],
            "correct_idx": r[6],
            "explanation": r[7],
        }
        for r in rows
    ]


@app.post("/api/questions")
@app.post("/questions")
async def add_question(req: QuestionCreate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (req.bank_id, req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation),
            )
        conn.commit()
    return {"status": "ok"}


@app.put("/api/questions/{q_id}")
@app.put("/questions/{q_id}")
async def update_question(q_id: int, req: QuestionUpdate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE questions
                   SET question=%s, opt1=%s, opt2=%s, opt3=%s, opt4=%s, correct_idx=%s, explanation=%s
                   WHERE id=%s""",
                (req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation, q_id),
            )
        conn.commit()
    return {"status": "ok"}


@app.post("/api/questions/{q_id}/move")
@app.post("/questions/{q_id}/move")
async def move_question(q_id: int, req: MoveQuestionReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE questions SET bank_id = %s WHERE id = %s", (req.target_bank_id, q_id))
        conn.commit()
    return {"status": "ok"}


@app.delete("/api/questions/{q_id}")
@app.delete("/questions/{q_id}")
async def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM questions WHERE id = %s", (q_id,))
        conn.commit()
    return {"status": "ok"}


@app.get("/api/communities")
@app.get("/communities")
async def get_communities(x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, title, chat_id, topic_id FROM communities ORDER BY id DESC")
            rows = c.fetchall()
    return [{"id": r[0], "title": r[1], "chat_id": r[2], "topic_id": r[3]} for r in rows]


@app.post("/api/communities")
@app.post("/communities")
async def add_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    title = req.title.strip()
    chat_id = req.chat_id.strip()
    if not title or not chat_id:
        raise HTTPException(status_code=400, detail="يرجى إدخال الاسم ومعرّف الوجهة.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO communities (title, chat_id, topic_id) VALUES (%s, %s, %s)",
                (title, chat_id, req.topic_id),
            )
        conn.commit()
    return {"status": "ok"}


@app.delete("/api/communities/{comm_id}")
@app.delete("/communities/{comm_id}")
async def delete_community(comm_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM communities WHERE id = %s", (comm_id,))
        conn.commit()
    return {"status": "ok"}


@app.post("/api/test_community")
@app.post("/test_community")
async def test_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    bot_app = build_bot_app()
    async with bot_app:
        try:
            kwargs = {"chat_id": req.chat_id.strip(), "text": "✅ تم التحقق من صلاحيات البوت والاتصال بالوجهة بنجاح."}
            if req.topic_id:
                kwargs["message_thread_id"] = req.topic_id
            await bot_app.bot.send_message(**kwargs)
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"تعذر الإرسال: تأكد من إضافة البوت كمشرف ومنح صلاحية النشر. ({e})",
            )


async def bg_send_all_questions(
    q_ids: List[int],
    target_chat,
    target_label: str,
    topic_id: Optional[int],
    delay_sec: float,
    shuffle_options: bool,
    open_period: Optional[int],
    admin_id: int,
):
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE id = ANY(%s)",
                (q_ids,),
            )
            q_map = {r[0]: r for r in c.fetchall()}

    ordered_rows = [q_map[qid] for qid in q_ids if qid in q_map]
    total_requested = len(ordered_rows)
    sent_count = 0
    failed_count = 0

    is_group_or_channel = str(target_chat).startswith("-") or str(target_chat).startswith("@")
    bot_app = build_bot_app()
    async with bot_app:
        for idx, row in enumerate(ordered_rows):
            _, q_text, o1, o2, o3, o4, c_idx, expl = row
            opts = [o1[:98], o2[:98], o3[:98], o4[:98]]
            current_correct = c_idx

            if shuffle_options:
                indexed = list(enumerate(opts))
                random.shuffle(indexed)
                opts = [x[1] for x in indexed]
                current_correct = [i for i, x in enumerate(indexed) if x[0] == c_idx][0]

            kwargs = {
                "chat_id": target_chat,
                "question": q_text[:295],
                "options": opts,
                "type": Poll.QUIZ,
                "correct_option_id": current_correct,
                "is_anonymous": True,
            }
            if expl:
                kwargs["explanation"] = expl[:195]
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            if open_period and 5 <= open_period <= 600:
                kwargs["open_period"] = open_period

            delivered = False
            while True:
                try:
                    await bot_app.bot.send_poll(**kwargs)
                    sent_count += 1
                    delivered = True
                    break
                except RetryAfter as e:
                    await asyncio.sleep(int(e.retry_after) + 1)
                except Exception as e:
                    err_msg = str(e)
                    if "retry after" in err_msg.lower() or "too many requests" in err_msg.lower():
                        m = re.search(r"(\d+)", err_msg)
                        await asyncio.sleep(int(m.group(1)) + 1 if m else 10)
                    else:
                        failed_count += 1
                        break

            if delivered and idx < total_requested - 1:
                if delay_sec > 0:
                    await asyncio.sleep(delay_sec)
                elif is_group_or_channel:
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.01)

        try:
            summary_lines = [
                "📊 **تقرير إرسال الاختبار**",
                f"• **الوجهة:** {target_label}",
                f"• **الأسئلة المرسلة بنجاح:** **{sent_count}** من **{total_requested}**",
            ]
            if failed_count > 0:
                summary_lines.append(f"• **الأسئلة المتعذرة:** **{failed_count}**")
            await bot_app.bot.send_message(
                chat_id=admin_id,
                text="\n".join(summary_lines),
                parse_mode="Markdown",
            )
        except Exception:
            pass


@app.post("/api/publish_batch")
@app.post("/publish_batch")
async def publish_batch(
    req: BatchPublishReq,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str = Header(default=""),
):
    user = verify_telegram_init_data(x_telegram_init_data)
    admin_id = user.get("id")
    target_chat = admin_id if req.target_type == "private" else (req.chat_id.strip() if req.chat_id else None)
    if not target_chat:
        raise HTTPException(status_code=400, detail="يرجى تحديد وجهة الإرسال أولاً.")

    if not req.question_ids:
        raise HTTPException(status_code=400, detail="لم يتم تحديد أي سؤال للإرسال.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*)::int FROM questions WHERE id = ANY(%s)", (req.question_ids,))
            valid_count = c.fetchone()[0]

    target_label = (
        "المحادثة الخاصة"
        if req.target_type == "private"
        else (req.target_name.strip() if req.target_name else str(target_chat))
    )

    background_tasks.add_task(
        bg_send_all_questions,
        req.question_ids,
        target_chat,
        target_label,
        req.topic_id,
        req.delay_sec,
        req.shuffle_options,
        req.open_period,
        admin_id,
    )
    return {
        "status": "queued",
        "queued_count": valid_count,
        "target_label": target_label,
    }


@app.get("/api/admins")
@app.get("/admins")
async def list_admins(x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    owner = get_owner_id()
    if user.get("id") != owner:
        raise HTTPException(status_code=403, detail="هذا القسم مخصص للمالك فقط.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT user_id, name FROM admins ORDER BY user_id ASC")
            rows = c.fetchall()
    return [{"user_id": r[0], "name": r[1], "is_owner": r[0] == owner} for r in rows]


@app.post("/api/admins")
@app.post("/admins")
async def add_admin(req: AdminAddReq, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    if user.get("id") != get_owner_id():
        raise HTTPException(status_code=403, detail="هذا القسم مخصص للمالك فقط.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO admins (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name",
                (req.user_id, req.name.strip() or "مشرف"),
            )
        conn.commit()
    return {"status": "ok"}


@app.delete("/api/admins/{admin_id}")
@app.delete("/admins/{admin_id}")
async def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    owner = get_owner_id()
    if user.get("id") != owner or admin_id == owner:
        raise HTTPException(status_code=403, detail="غير مسموح بهذا الإجراء.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM admins WHERE user_id = %s", (admin_id,))
        conn.commit()
    return {"status": "ok"}


@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def catch_all(full_path: str, request: Request):
    if "setup_webhook" in full_path:
        return await setup_webhook(request)
    if "webhook" in full_path and request.method == "POST":
        return await telegram_webhook(request)
    raise HTTPException(status_code=404, detail=f"المسار غير موجود: /{full_path}")
