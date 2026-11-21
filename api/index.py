import os
import io
import re
import json
import hmac
import hashlib
import urllib.parse
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
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

app = FastAPI(title="Quiz Management & Governance System")

db_pool: Optional[pool.ThreadedConnectionPool] = None
_db_initialized = False

DEFAULT_ADMIN_PERMS = {
    "tag": "مشرف",
    "can_create_bank": True,
    "can_edit_others_banks": False,
    "can_delete": False,
    "can_publish": True,
    "can_manage_admins": False,
}


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
            c.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    tag TEXT DEFAULT 'مشرف',
                    can_create_bank BOOLEAN DEFAULT TRUE,
                    can_edit_others_banks BOOLEAN DEFAULT FALSE,
                    can_delete BOOLEAN DEFAULT FALSE,
                    can_publish BOOLEAN DEFAULT TRUE,
                    can_manage_admins BOOLEAN DEFAULT FALSE
                )
            """)
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS tag TEXT DEFAULT 'مشرف'")
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS can_create_bank BOOLEAN DEFAULT TRUE")
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS can_edit_others_banks BOOLEAN DEFAULT FALSE")
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS can_delete BOOLEAN DEFAULT FALSE")
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS can_publish BOOLEAN DEFAULT TRUE")
            c.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS can_manage_admins BOOLEAN DEFAULT FALSE")

            c.execute("""
                CREATE TABLE IF NOT EXISTS banks (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE,
                    owner_id BIGINT DEFAULT NULL,
                    owner_name TEXT DEFAULT 'المالك'
                )
            """)
            c.execute("ALTER TABLE banks ADD COLUMN IF NOT EXISTS owner_id BIGINT DEFAULT NULL")
            c.execute("ALTER TABLE banks ADD COLUMN IF NOT EXISTS owner_name TEXT DEFAULT 'المالك'")

            c.execute("""
                CREATE TABLE IF NOT EXISTS questions (
                    id SERIAL PRIMARY KEY,
                    bank_id INTEGER REFERENCES banks(id) ON DELETE CASCADE,
                    question TEXT,
                    opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT,
                    correct_idx INTEGER,
                    explanation TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS communities (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    chat_id TEXT,
                    topic_id INTEGER DEFAULT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    actor_id BIGINT,
                    actor_name TEXT,
                    actor_role TEXT,
                    actor_tag TEXT,
                    action_type TEXT,
                    description TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank_id ON questions(bank_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
        conn.commit()
    _db_initialized = True


@app.on_event("startup")
async def on_startup():
    try:
        init_db()
        cleanup_old_logs()
    except Exception:
        pass


def cleanup_old_logs():
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM audit_logs WHERE created_at < %s", (cutoff,))
        conn.commit()


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
                """INSERT INTO admins (user_id, name, tag, can_create_bank, can_edit_others_banks, can_delete, can_publish, can_manage_admins)
                   VALUES (%s, %s, 'المالك', TRUE, TRUE, TRUE, TRUE, TRUE)
                   ON CONFLICT (user_id) DO UPDATE SET
                     name = EXCLUDED.name,
                     tag = 'المالك',
                     can_create_bank = TRUE,
                     can_edit_others_banks = TRUE,
                     can_delete = TRUE,
                     can_publish = TRUE,
                     can_manage_admins = TRUE""",
                (user_id, name),
            )
        conn.commit()


def get_actor_profile(user_id: int, fallback_name: str = "مستخدم") -> Optional[Dict[str, Any]]:
    owner = get_owner_id()
    if owner is None:
        return None
    if user_id == owner:
        return {
            "user_id": user_id,
            "name": fallback_name,
            "role": "owner",
            "role_label": "المالك",
            "tag": "المالك",
            "can_create_bank": True,
            "can_edit_others_banks": True,
            "can_delete": True,
            "can_publish": True,
            "can_manage_admins": True,
        }
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT user_id, name, tag, can_create_bank, can_edit_others_banks, can_delete, can_publish, can_manage_admins
                   FROM admins WHERE user_id = %s""",
                (user_id,),
            )
            row = c.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "name": row[1] or fallback_name,
        "role": "admin",
        "role_label": "مشرف",
        "tag": row[2] or "مشرف",
        "can_create_bank": bool(row[3]),
        "can_edit_others_banks": bool(row[4]),
        "can_delete": bool(row[5]),
        "can_publish": bool(row[6]),
        "can_manage_admins": bool(row[7]),
    }


def write_audit_log(actor: Dict[str, Any], action_type: str, description: str):
    cleanup_old_logs()
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO audit_logs (actor_id, actor_name, actor_role, actor_tag, action_type, description)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    actor.get("user_id"),
                    actor.get("name", "مستخدم"),
                    actor.get("role", "admin"),
                    actor.get("tag", "مشرف"),
                    action_type,
                    description,
                ),
            )
        conn.commit()


def verify_actor(init_data: str) -> Dict[str, Any]:
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
    first_name = user_info.get("first_name") or user_info.get("username") or "مستخدم"
    if not user_id:
        raise HTTPException(status_code=403, detail="⛔ تعذر قراءة معرف المستخدم.")

    if get_owner_id() is None:
        set_owner_id(user_id, first_name)

    profile = get_actor_profile(int(user_id), first_name)
    if not profile:
        raise HTTPException(status_code=403, detail="⛔ هذا النظام مخصص للمالك والمشرفين المعتمدين فقط.")
    return profile


def require_perm(actor: Dict[str, Any], perm_key: str, error_msg: str):
    if actor.get("role") == "owner":
        return
    if not actor.get(perm_key, False):
        raise HTTPException(status_code=403, detail=error_msg)


def get_bank_record(bank_id: int) -> Optional[Dict[str, Any]]:
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, name, owner_id, owner_name FROM banks WHERE id = %s", (bank_id,))
            row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "owner_id": row[2], "owner_name": row[3] or "المالك"}


def require_bank_edit_access(actor: Dict[str, Any], bank: Dict[str, Any]):
    if actor.get("role") == "owner":
        return
    bank_owner_id = bank.get("owner_id")
    if bank_owner_id is not None and int(bank_owner_id) == int(actor.get("user_id")):
        return
    if not actor.get("can_edit_others_banks", False):
        owner_label = bank.get("owner_name") or "مشرف آخر"
        raise HTTPException(
            status_code=403,
            detail=f"⛔ لا تملك صلاحية التعديل على بنك أنشأه ({owner_label}).",
        )


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
    comm_id: Optional[int] = None
    delay_sec: float = 0.0
    shuffle_options: bool = False
    open_period: Optional[int] = None
    limit_count: Optional[int] = None
    quiz_title: Optional[str] = None


class AdminUpsertReq(BaseModel):
    user_id: int
    name: Optional[str] = "مشرف"
    tag: Optional[str] = "مشرف"
    can_create_bank: bool = True
    can_edit_others_banks: bool = False
    can_delete: bool = False
    can_publish: bool = True
    can_manage_admins: bool = False


def build_bot_app(webapp_url: str = ""):
    application = Application.builder().token(BOT_TOKEN).build()

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        init_db()
        user = update.effective_user
        if get_owner_id() is None:
            set_owner_id(user.id, user.first_name)

        profile = get_actor_profile(user.id, user.first_name)
        if not profile:
            await update.message.reply_text("⛔ هذا البوت مخصص للمالك والمشرفين المعتمدين فقط.")
            return

        url = webapp_url or os.environ.get("RENDER_EXTERNAL_URL", "")
        keyboard = [[InlineKeyboardButton("🚀 فتح نظام إدارة الاختبارات", web_app=WebAppInfo(url=url))]]
        await update.message.reply_text(
            f"👋 أهلاً بك يا **{user.first_name}** ({profile['tag']})!\n\n"
            "• افتح لوحة التحكم لإدارة البنوك، الصلاحيات، الوجهات المحفوظة، وسجل العمليات.\n"
            "• أرسل `/id` داخل أي مجموعة أو موضوع (Topic) أو حوّل رسالة من القناة لاستخراج المعرّف.",
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
                    f"✅ **تم استخراج بيانات الوجهة:**\n"
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
    actor = verify_actor(x_telegram_init_data)
    return actor


@app.get("/api/banks")
@app.get("/banks")
async def get_banks(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT b.id, b.name, b.owner_id, COALESCE(b.owner_name, 'المالك'), COUNT(q.id)::int
                FROM banks b
                LEFT JOIN questions q ON q.bank_id = b.id
                GROUP BY b.id, b.name, b.owner_id, b.owner_name
                ORDER BY b.id DESC
            """)
            rows = c.fetchall()

    result = []
    for r in rows:
        b_owner_id = r[2]
        can_edit = (
            actor["role"] == "owner"
            or b_owner_id is None
            or int(b_owner_id) == int(actor["user_id"])
            or actor["can_edit_others_banks"]
        )
        can_del = actor["role"] == "owner" or (can_edit and actor["can_delete"])
        result.append({
            "id": r[0],
            "name": r[1],
            "owner_id": b_owner_id,
            "owner_name": r[3],
            "count": r[4],
            "can_edit": can_edit,
            "can_delete": can_del,
        })
    return result


@app.post("/api/banks")
@app.post("/banks")
async def create_bank(req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_create_bank", "⛔ لا تملك صلاحية إنشاء بنوك أسئلة جديدة.")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="يرجى إدخال اسم البنك.")

    actor_label = f"{actor['name']} ({actor['tag']})" if actor["role"] == "admin" else "المالك"
    with PooledConn() as conn:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO banks (name, owner_id, owner_name) VALUES (%s, %s, %s) RETURNING id",
                    (name, actor["user_id"], actor_label),
                )
                bank_id = c.fetchone()[0]
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=400, detail="يوجد بنك مسجل بهذا الاسم مسبقاً.")

    write_audit_log(actor, "create_bank", f"أنشأ بنك الأسئلة «{name}».")
    return {"id": bank_id, "name": name}


@app.put("/api/banks/{bank_id}")
@app.put("/banks/{bank_id}")
async def rename_bank(bank_id: int, req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    bank = get_bank_record(bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    new_name = req.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="الاسم الجديد لا يمكن أن يكون فارغاً.")

    old_name = bank["name"]
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE banks SET name = %s WHERE id = %s", (new_name, bank_id))
        conn.commit()

    write_audit_log(actor, "rename_bank", f"غيّر اسم البنك من «{old_name}» إلى «{new_name}».")
    return {"status": "ok"}


@app.delete("/api/banks/{bank_id}")
@app.delete("/banks/{bank_id}")
async def delete_bank(bank_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "⛔ لا تملك صلاحية الحذف.")
    bank = get_bank_record(bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM banks WHERE id = %s", (bank_id,))
        conn.commit()

    write_audit_log(actor, "delete_bank", f"حذف بنك الأسئلة «{bank['name']}» وجميع أسئلته.")
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
    actor = verify_actor(x_telegram_init_data)
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

    actor_label = f"{actor['name']} ({actor['tag']})" if actor["role"] == "admin" else "المالك"
    with PooledConn() as conn:
        try:
            with conn.cursor() as c:
                if bank_id is not None:
                    existing_bank = get_bank_record(bank_id)
                    if not existing_bank:
                        raise HTTPException(status_code=404, detail="البنك المستهدف غير موجود.")
                    require_bank_edit_access(actor, existing_bank)
                    target_bank_id = bank_id
                    target_bank_name = existing_bank["name"]
                else:
                    require_perm(actor, "can_create_bank", "⛔ لا تملك صلاحية إنشاء بنوك جديدة.")
                    clean_bank = bank_name.strip()
                    if not clean_bank:
                        raise HTTPException(status_code=400, detail="يرجى إدخال اسم البنك.")
                    c.execute(
                        "INSERT INTO banks (name, owner_id, owner_name) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
                        (clean_bank, actor["user_id"], actor_label),
                    )
                    c.execute("SELECT id, name FROM banks WHERE name = %s", (clean_bank,))
                    b_row = c.fetchone()
                    target_bank_id, target_bank_name = b_row[0], b_row[1]

                for q_text, o1, o2, o3, o4, c_idx, expl in rows_to_insert:
                    c.execute(
                        """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (target_bank_id, q_text, o1, o2, o3, o4, c_idx, expl),
                    )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=f"تعذر حفظ الأسئلة: {str(e)}")

    write_audit_log(
        actor,
        "import_csv",
        f"استورد {len(rows_to_insert)} سؤالاً من CSV إلى بنك «{target_bank_name}».",
    )
    return {"status": "ok", "added": len(rows_to_insert), "bank_id": target_bank_id}


@app.get("/api/banks/{bank_id}/questions")
@app.get("/banks/{bank_id}/questions")
async def get_questions(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
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
    actor = verify_actor(x_telegram_init_data)
    bank = get_bank_record(req.bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (req.bank_id, req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation),
            )
        conn.commit()

    write_audit_log(actor, "add_question", f"أضاف سؤالاً جديداً إلى بنك «{bank['name']}».")
    return {"status": "ok"}


@app.put("/api/questions/{q_id}")
@app.put("/questions/{q_id}")
async def update_question(q_id: int, req: QuestionUpdate, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT bank_id FROM questions WHERE id = %s", (q_id,))
            row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="السؤال غير موجود.")
    bank = get_bank_record(row[0])
    if bank:
        require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE questions
                   SET question=%s, opt1=%s, opt2=%s, opt3=%s, opt4=%s, correct_idx=%s, explanation=%s
                   WHERE id=%s""",
                (req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation, q_id),
            )
        conn.commit()

    write_audit_log(actor, "edit_question", f"عدّل سؤالاً في بنك «{bank['name'] if bank else q_id}».")
    return {"status": "ok"}


@app.post("/api/questions/{q_id}/move")
@app.post("/questions/{q_id}/move")
async def move_question(q_id: int, req: MoveQuestionReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT bank_id FROM questions WHERE id = %s", (q_id,))
            row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="السؤال غير موجود.")

    src_bank = get_bank_record(row[0])
    dst_bank = get_bank_record(req.target_bank_id)
    if not dst_bank:
        raise HTTPException(status_code=404, detail="البنك الهدف غير موجود.")
    if src_bank:
        require_bank_edit_access(actor, src_bank)
    require_bank_edit_access(actor, dst_bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE questions SET bank_id = %s WHERE id = %s", (req.target_bank_id, q_id))
        conn.commit()

    write_audit_log(
        actor,
        "move_question",
        f"نقل سؤالاً من بنك «{src_bank['name'] if src_bank else ''}» إلى بنك «{dst_bank['name']}».",
    )
    return {"status": "ok"}


@app.delete("/api/questions/{q_id}")
@app.delete("/questions/{q_id}")
async def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "⛔ لا تملك صلاحية حذف الأسئلة.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT bank_id FROM questions WHERE id = %s", (q_id,))
            row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="السؤال غير موجود.")
    bank = get_bank_record(row[0])
    if bank:
        require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM questions WHERE id = %s", (q_id,))
        conn.commit()

    write_audit_log(actor, "delete_question", f"حذف سؤالاً من بنك «{bank['name'] if bank else ''}».")
    return {"status": "ok"}


@app.get("/api/communities")
@app.get("/communities")
async def get_communities(x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, title, chat_id, topic_id FROM communities ORDER BY id DESC")
            rows = c.fetchall()
    return [{"id": r[0], "title": r[1], "chat_id": r[2], "topic_id": r[3]} for r in rows]


@app.post("/api/communities")
@app.post("/communities")
async def add_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    title = req.title.strip()
    chat_id = req.chat_id.strip()
    if not title or not chat_id:
        raise HTTPException(status_code=400, detail="يرجى إدخال اسم الوجهة ومعرّفها.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO communities (title, chat_id, topic_id) VALUES (%s, %s, %s)",
                (title, chat_id, req.topic_id),
            )
        conn.commit()

    write_audit_log(actor, "add_community", f"أضاف وجهة إرسال باسم «{title}».")
    return {"status": "ok"}


@app.delete("/api/communities/{comm_id}")
@app.delete("/communities/{comm_id}")
async def delete_community(comm_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "⛔ لا تملك صلاحية حذف الوجهات.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT title FROM communities WHERE id = %s", (comm_id,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="الوجهة غير موجودة.")
            c.execute("DELETE FROM communities WHERE id = %s", (comm_id,))
        conn.commit()

    write_audit_log(actor, "delete_community", f"حذف وجهة الإرسال «{row[0]}».")
    return {"status": "ok"}


@app.post("/api/test_community")
@app.post("/test_community")
async def test_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
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
    quiz_title: Optional[str],
    actor: Dict[str, Any],
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
    admin_id = actor["user_id"]

    is_group_or_channel = str(target_chat).startswith("-") or str(target_chat).startswith("@")
    bot_app = build_bot_app()
    async with bot_app:
        if quiz_title and quiz_title.strip() and total_requested > 0:
            try:
                intro_kwargs = {
                    "chat_id": target_chat,
                    "text": f"📝 **{quiz_title.strip()}**\nعدد الأسئلة: **{total_requested}**",
                    "parse_mode": "Markdown",
                }
                if topic_id:
                    intro_kwargs["message_thread_id"] = topic_id
                await bot_app.bot.send_message(**intro_kwargs)
            except Exception:
                pass

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
                f"• **المرسل فعلياً:** **{sent_count}** من **{total_requested}**",
            ]
            if failed_count > 0:
                summary_lines.append(f"• **المتعذر إرساله:** **{failed_count}**")
            await bot_app.bot.send_message(
                chat_id=admin_id,
                text="\n".join(summary_lines),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    actor_desc = f"{actor['name']} ({actor['tag']})" if actor["role"] == "admin" else "المالك"
    write_audit_log(
        actor,
        "publish_quiz",
        f"قام {actor_desc} بإرسال {sent_count} من {total_requested} سؤالاً إلى «{target_label}».",
    )


@app.post("/api/publish_batch")
@app.post("/publish_batch")
async def publish_batch(
    req: BatchPublishReq,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "⛔ لا تملك صلاحية إرسال الاختبارات.")

    q_ids = list(req.question_ids or [])
    if req.limit_count and req.limit_count > 0:
        q_ids = q_ids[: req.limit_count]

    if not q_ids:
        raise HTTPException(status_code=400, detail="لم يتم تحديد أي سؤال للإرسال.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id FROM questions WHERE id = ANY(%s)", (q_ids,))
            valid_set = {r[0] for r in c.fetchall()}
            if req.target_type == "saved_comm":
                if not req.comm_id:
                    raise HTTPException(status_code=400, detail="يرجى اختيار وجهة محفوظة.")
                c.execute("SELECT title, chat_id, topic_id FROM communities WHERE id = %s", (req.comm_id,))
                comm_row = c.fetchone()
                if not comm_row:
                    raise HTTPException(status_code=404, detail="الوجهة المحفوظة غير موجودة.")
                target_chat = comm_row[1].strip()
                target_label = comm_row[0]
                topic_id = comm_row[2]
            elif req.target_type == "private":
                target_chat = actor["user_id"]
                target_label = "المحادثة الخاصة"
                topic_id = None
            else:
                raise HTTPException(status_code=400, detail="نوع الوجهة غير مدعوم.")

    ordered_valid_ids = [qid for qid in q_ids if qid in valid_set]
    if not ordered_valid_ids:
        raise HTTPException(status_code=400, detail="الأسئلة المحددة غير متوفرة.")

    background_tasks.add_task(
        bg_send_all_questions,
        ordered_valid_ids,
        target_chat,
        target_label,
        topic_id,
        req.delay_sec,
        req.shuffle_options,
        req.open_period,
        req.quiz_title,
        actor,
    )
    return {
        "status": "queued",
        "queued_count": len(ordered_valid_ids),
        "target_label": target_label,
    }


@app.get("/api/admins")
@app.get("/admins")
async def list_admins(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "⛔ إدارة المشرفين غير متاحة لصلاحياتك الحالية.")
    owner = get_owner_id()
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT user_id, name, tag, can_create_bank, can_edit_others_banks, can_delete, can_publish, can_manage_admins
                   FROM admins ORDER BY user_id ASC"""
            )
            rows = c.fetchall()

    return [
        {
            "user_id": r[0],
            "name": r[1],
            "tag": r[2] or ("المالك" if r[0] == owner else "مشرف"),
            "is_owner": r[0] == owner,
            "can_create_bank": bool(r[3]),
            "can_edit_others_banks": bool(r[4]),
            "can_delete": bool(r[5]),
            "can_publish": bool(r[6]),
            "can_manage_admins": bool(r[7]),
        }
        for r in rows
    ]


@app.post("/api/admins")
@app.post("/admins")
async def upsert_admin(req: AdminUpsertReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "⛔ لا تملك صلاحية إدارة المشرفين.")
    owner = get_owner_id()
    if req.user_id == owner and actor["role"] != "owner":
        raise HTTPException(status_code=403, detail="لا يمكن تعديل بيانات المالك.")

    clean_name = (req.name or "مشرف").strip()
    clean_tag = "المالك" if req.user_id == owner else ((req.tag or "مشرف").strip() or "مشرف")
    can_mgr = True if req.user_id == owner else (bool(req.can_manage_admins) if actor["role"] == "owner" else False)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM admins WHERE user_id = %s", (req.user_id,))
            existed = c.fetchone() is not None
            c.execute(
                """INSERT INTO admins (user_id, name, tag, can_create_bank, can_edit_others_banks, can_delete, can_publish, can_manage_admins)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET
                     name = EXCLUDED.name,
                     tag = EXCLUDED.tag,
                     can_create_bank = EXCLUDED.can_create_bank,
                     can_edit_others_banks = EXCLUDED.can_edit_others_banks,
                     can_delete = EXCLUDED.can_delete,
                     can_publish = EXCLUDED.can_publish,
                     can_manage_admins = EXCLUDED.can_manage_admins""",
                (
                    req.user_id,
                    clean_name,
                    clean_tag,
                    bool(req.can_create_bank),
                    bool(req.can_edit_others_banks),
                    bool(req.can_delete),
                    bool(req.can_publish),
                    can_mgr,
                ),
            )
        conn.commit()

    verb = "حدّث صلاحيات ووسم" if existed else "أضاف المشرف"
    write_audit_log(actor, "manage_admin", f"{verb} «{clean_name}» ({clean_tag}).")
    return {"status": "ok"}


@app.delete("/api/admins/{admin_id}")
@app.delete("/admins/{admin_id}")
async def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "⛔ لا تملك صلاحية إزالة المشرفين.")
    owner = get_owner_id()
    if admin_id == owner:
        raise HTTPException(status_code=403, detail="لا يمكن إزالة حساب المالك.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT name, tag FROM admins WHERE user_id = %s", (admin_id,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="المشرف غير موجود.")
            c.execute("DELETE FROM admins WHERE user_id = %s", (admin_id,))
        conn.commit()

    write_audit_log(actor, "remove_admin", f"أزال المشرف «{row[0]}» ({row[1] or 'مشرف'}).")
    return {"status": "ok"}


@app.get("/api/logs")
@app.get("/logs")
async def get_logs(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    cleanup_old_logs()
    with PooledConn() as conn:
        with conn.cursor() as c:
            if actor["role"] == "owner" or actor["can_manage_admins"]:
                c.execute(
                    """SELECT id, actor_name, actor_role, actor_tag, action_type, description, created_at
                       FROM audit_logs ORDER BY id DESC LIMIT 120"""
                )
            else:
                c.execute(
                    """SELECT id, actor_name, actor_role, actor_tag, action_type, description, created_at
                       FROM audit_logs WHERE actor_id = %s ORDER BY id DESC LIMIT 80""",
                    (actor["user_id"],),
                )
            rows = c.fetchall()

    return [
        {
            "id": r[0],
            "actor_name": r[1],
            "actor_role": r[2],
            "actor_tag": r[3] or ("المالك" if r[2] == "owner" else "مشرف"),
            "action_type": r[4],
            "description": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


@app.delete("/api/logs")
@app.delete("/logs")
async def clear_logs(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    if actor["role"] != "owner":
        raise HTTPException(status_code=403, detail="مسح السجل بالكامل متاح للمالك فقط.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM audit_logs")
        conn.commit()
    return {"status": "ok"}


@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def catch_all(full_path: str, request: Request):
    if "setup_webhook" in full_path:
        return await setup_webhook(request)
    if "webhook" in full_path and request.method == "POST":
        return await telegram_webhook(request)
    raise HTTPException(status_code=404, detail=f"المسار غير موجود: /{full_path}")
