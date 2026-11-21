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
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll, MenuButtonWebApp, Bot
from telegram.error import RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9268644584:AAE8woNBoXzueYqpsT4x69efYcDgP1mPP9Y").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="Quiz Management & Governance System")

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
                    payload JSONB DEFAULT NULL,
                    undone BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            c.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT NULL")
            c.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS undone BOOLEAN DEFAULT FALSE")

            c.execute("""
                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id SERIAL PRIMARY KEY,
                    actor_id BIGINT,
                    actor_name TEXT,
                    actor_tag TEXT,
                    bank_id INTEGER,
                    target_type TEXT,
                    comm_id INTEGER,
                    target_label TEXT,
                    quiz_title TEXT DEFAULT '',
                    total_count INTEGER DEFAULT 0,
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    failed_question_ids JSONB DEFAULT '[]'::jsonb,
                    last_error TEXT DEFAULT '',
                    delay_sec REAL DEFAULT 0.0,
                    shuffle_options BOOLEAN DEFAULT FALSE,
                    open_period INTEGER DEFAULT NULL,
                    status TEXT DEFAULT 'queued',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    completed_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
                )
            """)
            c.execute("ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS last_error TEXT DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank_id ON questions(bank_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_publish_jobs_id ON publish_jobs(id DESC)")
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
            c.execute("DELETE FROM publish_jobs WHERE created_at < %s", (cutoff,))
        conn.commit()


def replace_with_single_job(keep_job_id: Optional[int] = None):
    with PooledConn() as conn:
        with conn.cursor() as c:
            if keep_job_id is not None:
                c.execute("DELETE FROM publish_jobs WHERE id != %s", (keep_job_id,))
            else:
                c.execute("DELETE FROM publish_jobs")
        conn.commit()


def get_owner_id() -> Optional[int]:
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM config WHERE key = 'owner_id'")
            row = c.fetchone()
    return int(row[0]) if row else None


def set_owner_id(user_id: int, name: str = "المالك"):
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


def format_display_name(user_info: dict) -> str:
    first = (user_info.get("first_name") or "").strip()
    last = (user_info.get("last_name") or "").strip()
    username = (user_info.get("username") or "").strip()
    full_name = f"{first} {last}".strip()
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return "مستخدم"


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


def write_audit_log(
    actor: Dict[str, Any],
    action_type: str,
    description: str,
    payload: Optional[Dict[str, Any]] = None,
):
    init_db()
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO audit_logs (actor_id, actor_name, actor_role, actor_tag, action_type, description, payload, undone)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, FALSE)""",
                (
                    actor.get("user_id"),
                    actor.get("name", "مستخدم"),
                    actor.get("role", "admin"),
                    actor.get("tag", "مشرف"),
                    action_type,
                    description,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
        conn.commit()


def verify_actor(init_data: str) -> Dict[str, Any]:
    init_db()
    if not init_data or not init_data.strip():
        raise HTTPException(status_code=403, detail="غير مصرح: يجب فتح التطبيق من داخل تيليجرام.")

    parsed = dict(urllib.parse.parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        raise HTTPException(status_code=403, detail="توقيع تيليجرام مفقود.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if calculated_hash != hash_val:
        raise HTTPException(status_code=403, detail="فشل التحقق الأمني من هوية تيليجرام.")

    user_info = json.loads(parsed.get("user", "{}"))
    user_id = user_info.get("id")
    display_name = format_display_name(user_info)
    if not user_id:
        raise HTTPException(status_code=403, detail="تعذر قراءة معرف المستخدم.")

    if get_owner_id() is None:
        set_owner_id(int(user_id), display_name)

    profile = get_actor_profile(int(user_id), display_name)
    if not profile:
        raise HTTPException(status_code=403, detail="عذراً، أنت لست مشرفاً في البوت.")
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
            detail=f"لا تملك صلاحية التعديل على بنك أنشأه ({owner_label}).",
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


class TransferQuestionReq(BaseModel):
    target_bank_id: int


class BankRenameReq(BaseModel):
    name: str


class CommunityCreateReq(BaseModel):
    title: str
    chat_id: str
    topic_id: Optional[int] = None


class BatchPublishReq(BaseModel):
    bank_id: Optional[int] = None
    question_ids: List[int]
    target_type: str
    comm_id: Optional[int] = None
    delay_sec: float = 0.0
    shuffle_options: bool = False
    open_period: Optional[int] = None
    limit_count: Optional[int] = None
    quiz_title: Optional[str] = None


class JobControlReq(BaseModel):
    job_id: int
    action: str


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
        display_name = format_display_name({
            "first_name": getattr(user, "first_name", ""),
            "last_name": getattr(user, "last_name", ""),
            "username": getattr(user, "username", ""),
        })

        if get_owner_id() is None:
            set_owner_id(user.id, display_name)

        profile = get_actor_profile(user.id, display_name)
        if not profile:
            await update.message.reply_text("عذراً، أنت لست مشرفاً في البوت.")
            return

        url = webapp_url or os.environ.get("RENDER_EXTERNAL_URL", "")
        keyboard = [[InlineKeyboardButton("فتح لوحة إدارة الاختبارات", web_app=WebAppInfo(url=url))]]
        await update.message.reply_text(
            f"أهلاً بك يا **{display_name}** ({profile['tag']}).\nاضغط على الزر أدناه لفتح لوحة التحكم.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    application.add_handler(CommandHandler("start", start_cmd))
    return application


@app.get("/api/setup_webhook")
@app.get("/setup_webhook")
async def setup_webhook(request: Request):
    init_db()
    base_url = get_base_url(request)
    webhook_url = f"{base_url}/api/webhook"

    bot_app = build_bot_app(base_url)
    await bot_app.initialize()
    try:
        await bot_app.bot.set_webhook(url=webhook_url)
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="إدارة الاختبارات", web_app=WebAppInfo(url=base_url))
        )
    finally:
        await bot_app.shutdown()

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
    await bot_app.initialize()
    try:
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
    finally:
        await bot_app.shutdown()
    return {"ok": True}


@app.get("/api/me")
@app.get("/me")
async def get_me(x_telegram_init_data: str = Header(default="")):
    return verify_actor(x_telegram_init_data)


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
    require_perm(actor, "can_create_bank", "لا تملك صلاحية إنشاء بنوك أسئلة جديدة.")
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
    require_perm(actor, "can_delete", "لا تملك صلاحية الحذف.")
    bank = get_bank_record(bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC",
                (bank_id,),
            )
            q_rows = c.fetchall()
            snapshot_questions = [
                {
                    "question": r[0],
                    "opt1": r[1],
                    "opt2": r[2],
                    "opt3": r[3],
                    "opt4": r[4],
                    "correct_idx": r[5],
                    "explanation": r[6] or "",
                }
                for r in q_rows
            ]
            c.execute("DELETE FROM banks WHERE id = %s", (bank_id,))
        conn.commit()

    snapshot_payload = {
        "undo_kind": "restore_bank",
        "bank_name": bank["name"],
        "owner_id": bank["owner_id"],
        "owner_name": bank["owner_name"],
        "questions": snapshot_questions,
    }
    write_audit_log(
        actor,
        "delete_bank",
        f"حذف بنك الأسئلة «{bank['name']}» ({len(snapshot_questions)} سؤالاً).",
        payload=snapshot_payload,
    )
    return {"status": "ok"}


@app.post("/api/logs/{log_id}/undo")
@app.post("/logs/{log_id}/undo")
async def undo_audit_log(log_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, actor_id, action_type, payload, undone FROM audit_logs WHERE id = %s",
                (log_id,),
            )
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="السجل غير موجود.")
            if row[4]:
                raise HTTPException(status_code=400, detail="تم تنفيذ التراجع على هذا السجل مسبقاً.")

            payload = row[3] if isinstance(row[3], dict) else (json.loads(row[3]) if row[3] else None)
            if not payload or payload.get("undo_kind") != "restore_bank":
                raise HTTPException(status_code=400, detail="هذه العملية لا تدعم التراجع التلقائي.")

            orig_owner_id = payload.get("owner_id")
            if actor["role"] != "owner" and not actor["can_edit_others_banks"]:
                if orig_owner_id is not None and int(orig_owner_id) != int(actor["user_id"]):
                    raise HTTPException(status_code=403, detail="لا تملك صلاحية استعادة بنك يخص مشرفاً آخر.")

            restored_name = payload["bank_name"]
            c.execute("SELECT id FROM banks WHERE name = %s", (restored_name,))
            if c.fetchone():
                restored_name = f"{restored_name} (مستعاد)"

            c.execute(
                "INSERT INTO banks (name, owner_id, owner_name) VALUES (%s, %s, %s) RETURNING id",
                (
                    restored_name,
                    payload.get("owner_id") or actor["user_id"],
                    payload.get("owner_name") or actor["name"],
                ),
            )
            new_bank_id = c.fetchone()[0]

            q_list = payload.get("questions") or []
            for q in q_list:
                c.execute(
                    """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        new_bank_id,
                        q["question"],
                        q["opt1"],
                        q["opt2"],
                        q["opt3"],
                        q["opt4"],
                        q["correct_idx"],
                        q.get("explanation", ""),
                    ),
                )

            c.execute("UPDATE audit_logs SET undone = TRUE WHERE id = %s", (log_id,))
        conn.commit()

    write_audit_log(
        actor,
        "undo_delete_bank",
        f"استعاد بنك الأسئلة المحذوف «{restored_name}» ({len(q_list)} سؤالاً).",
    )
    return {"status": "ok", "bank_id": new_bank_id, "restored_questions": len(q_list)}


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
                    require_perm(actor, "can_create_bank", "لا تملك صلاحية إنشاء بنوك جديدة.")
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
@app.post("/api/questions/{q_id}/move")
async def move_question(q_id: int, req: TransferQuestionReq, x_telegram_init_data: str = Header(default="")):
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


@app.post("/api/questions/{q_id}/copy")
@app.post("/api/questions/{q_id}/copy")
async def copy_question(q_id: int, req: TransferQuestionReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE id = %s",
                (q_id,),
            )
            row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="السؤال غير موجود.")

    src_bank = get_bank_record(row[0])
    dst_bank = get_bank_record(req.target_bank_id)
    if not dst_bank:
        raise HTTPException(status_code=404, detail="البنك الهدف غير موجود.")
    require_bank_edit_access(actor, dst_bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (req.target_bank_id, row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
            )
        conn.commit()

    write_audit_log(
        actor,
        "copy_question",
        f"نسخ سؤالاً من بنك «{src_bank['name'] if src_bank else ''}» إلى بنك «{dst_bank['name']}».",
    )
    return {"status": "ok"}


@app.delete("/api/questions/{q_id}")
@app.delete("/api/questions/{q_id}")
async def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "لا تملك صلاحية حذف الأسئلة.")
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
@app.delete("/api/communities/{comm_id}")
async def delete_community(comm_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "لا تملك صلاحية حذف الوجهات.")
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
    raw_chat = req.chat_id.strip()
    parsed_chat: Any = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat
    try:
        async with Bot(token=BOT_TOKEN) as bot:
            kwargs: Dict[str, Any] = {"chat_id": parsed_chat, "text": "تم التحقق من صلاحيات البوت والاتصال بالوجهة بنجاح."}
            if req.topic_id:
                kwargs["message_thread_id"] = int(req.topic_id)
            await bot.send_message(**kwargs)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"تعذر الإرسال: تأكد من إضافة البوت كمشرف ومنح صلاحية النشر. ({e})",
        )


def _sanitize_poll_options(o1: Any, o2: Any, o3: Any, o4: Any) -> List[str]:
    raw_opts = [o1, o2, o3, o4]
    cleaned: List[str] = []
    seen = set()
    for idx, val in enumerate(raw_opts):
        txt = str(val if val is not None else "").strip()
        if not txt:
            txt = f"خيار {idx + 1}"
        txt = txt[:95]
        if txt in seen:
            txt = f"{txt[:90]} ({idx + 1})"
        seen.add(txt)
        cleaned.append(txt)
    return cleaned[:4]


@app.post("/api/publish_batch")
@app.post("/publish_batch")
async def publish_batch(
    req: BatchPublishReq,
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "لا تملك صلاحية إرسال الاختبارات.")

    q_ids = list(req.question_ids or [])
    if req.limit_count and req.limit_count > 0:
        q_ids = q_ids[: req.limit_count]

    if not q_ids:
        raise HTTPException(status_code=400, detail="لم يتم تحديد أي سؤال للإرسال.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE id = ANY(%s)", (q_ids,))
            q_rows = c.fetchall()
            q_map = {r[0]: r for r in q_rows}

            if req.target_type == "saved_comm":
                if not req.comm_id:
                    raise HTTPException(status_code=400, detail="يرجى اختيار وجهة محفوظة.")
                c.execute("SELECT title, chat_id, topic_id FROM communities WHERE id = %s", (req.comm_id,))
                comm_row = c.fetchone()
                if not comm_row:
                    raise HTTPException(status_code=404, detail="الوجهة المحفوظة غير موجودة.")
                target_chat_raw = comm_row[1].strip()
                target_label = comm_row[0]
                topic_id = comm_row[2]
            elif req.target_type == "private":
                target_chat_raw = str(actor["user_id"])
                target_label = "المحادثة الخاصة"
                topic_id = None
            else:
                raise HTTPException(status_code=400, detail="نوع الوجهة غير مدعوم.")

    ordered_rows = [q_map[qid] for qid in q_ids if qid in q_map]
    if not ordered_rows:
        raise HTTPException(status_code=400, detail="الأسئلة المحددة غير متوفرة.")

    parsed_chat: Any = int(target_chat_raw) if target_chat_raw.lstrip("-").isdigit() else target_chat_raw
    total_requested = len(ordered_rows)
    sent_count = 0
    failed_count = 0
    failed_ids: List[int] = []
    last_err = ""
    active_topic_id: Optional[int] = int(topic_id) if topic_id else None

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM publish_jobs")
            c.execute(
                """INSERT INTO publish_jobs
                   (actor_id, actor_name, actor_tag, bank_id, target_type, comm_id, target_label,
                    quiz_title, total_count, sent_count, failed_count, failed_question_ids, last_error,
                    delay_sec, shuffle_options, open_period, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, '[]'::jsonb, '', %s, %s, %s, 'running')
                   RETURNING id""",
                (
                    actor["user_id"],
                    actor["name"],
                    actor["tag"],
                    req.bank_id,
                    req.target_type,
                    req.comm_id,
                    target_label,
                    (req.quiz_title or "").strip(),
                    total_requested,
                    req.delay_sec,
                    req.shuffle_options,
                    req.open_period,
                ),
            )
            job_id = c.fetchone()[0]
        conn.commit()

    replace_with_single_job(keep_job_id=int(job_id))

    if req.quiz_title and req.quiz_title.strip():
        try:
            async with Bot(token=BOT_TOKEN) as bot:
                intro_kwargs: Dict[str, Any] = {
                    "chat_id": parsed_chat,
                    "text": f"{req.quiz_title.strip()}\nعدد الأسئلة: {total_requested}",
                }
                if active_topic_id:
                    intro_kwargs["message_thread_id"] = active_topic_id
                await bot.send_message(**intro_kwargs)
        except Exception as e:
            if active_topic_id and "thread" in str(e).lower():
                active_topic_id = None
                try:
                    async with Bot(token=BOT_TOKEN) as bot:
                        await bot.send_message(
                            chat_id=parsed_chat,
                            text=f"{req.quiz_title.strip()}\nعدد الأسئلة: {total_requested}",
                        )
                except Exception:
                    pass

    for idx, row in enumerate(ordered_rows):
        qid, q_text, o1, o2, o3, o4, c_idx, expl = row
        opts = _sanitize_poll_options(o1, o2, o3, o4)
        current_correct = int(c_idx or 0)
        if current_correct < 0 or current_correct >= len(opts):
            current_correct = 0

        if req.shuffle_options:
            indexed = list(enumerate(opts))
            random.shuffle(indexed)
            opts = [x[1] for x in indexed]
            current_correct = [i for i, x in enumerate(indexed) if x[0] == current_correct][0]

        q_str = str(q_text or "").strip()[:290]
        if not q_str:
            q_str = "سؤال"

        delivered = False
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                async with Bot(token=BOT_TOKEN) as bot:
                    kwargs: Dict[str, Any] = {
                        "chat_id": parsed_chat,
                        "question": q_str,
                        "options": opts,
                        "type": Poll.QUIZ,
                        "correct_option_id": current_correct,
                        "is_anonymous": True,
                    }
                    if expl and str(expl).strip():
                        kwargs["explanation"] = str(expl).strip()[:190]
                    if active_topic_id:
                        kwargs["message_thread_id"] = active_topic_id
                    if req.open_period is not None and 5 <= int(req.open_period) <= 600:
                        kwargs["open_period"] = int(req.open_period)
                    await bot.send_poll(**kwargs)
                sent_count += 1
                delivered = True
                break
            except RetryAfter as e:
                last_err = f"RetryAfter: {e.retry_after}"
                await asyncio.sleep(min(int(e.retry_after) + 1, 15))
            except TelegramError as e:
                err_msg = str(e)
                last_err = err_msg
                low = err_msg.lower()
                if "thread" in low and active_topic_id:
                    active_topic_id = None
                    continue
                if "retry after" in low or "too many requests" in low or "timed out" in low:
                    m = re.search(r"(\d+)", err_msg)
                    await asyncio.sleep(min(int(m.group(1)) + 1 if m else 3, 15))
                else:
                    break
            except Exception as e:
                last_err = str(e)
                if "timed out" in last_err.lower() or "timeout" in last_err.lower():
                    await asyncio.sleep(1.5)
                else:
                    break

        if not delivered:
            failed_count += 1
            failed_ids.append(qid)

        if idx < total_requested - 1 and req.delay_sec and req.delay_sec > 0:
            await asyncio.sleep(req.delay_sec)

    final_status = "completed" if failed_count == 0 else ("partial_failed" if sent_count > 0 else "failed")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE publish_jobs
                   SET status = %s, sent_count = %s, failed_count = %s,
                       failed_question_ids = %s::jsonb, last_error = %s, completed_at = NOW()
                   WHERE id = %s""",
                (final_status, sent_count, failed_count, json.dumps(failed_ids), last_err[:250], job_id),
            )
        conn.commit()

    if sent_count > 0:
        write_audit_log(
            actor,
            "publish_batch",
            f"أرسل {sent_count} من أصل {total_requested} سؤالاً إلى «{target_label}».",
        )

    return {
        "status": final_status,
        "job_id": job_id,
        "queued_count": total_requested,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "failed_question_ids": failed_ids,
        "last_error": last_err,
        "target_label": target_label,
    }


@app.get("/api/publish_jobs")
@app.get("/publish_jobs")
async def get_publish_jobs(x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, actor_name, actor_tag, target_label, quiz_title,
                       total_count, sent_count, failed_count, failed_question_ids,
                       last_error, status, created_at, completed_at
                FROM publish_jobs ORDER BY id DESC LIMIT 1
            """)
            rows = c.fetchall()

    jobs = []
    for r in rows:
        f_ids = r[8] if isinstance(r[8], list) else (json.loads(r[8]) if r[8] else [])
        total_c = r[5] or 0
        sent_c = r[6] or 0
        failed_c = r[7] or 0
        rate = round((sent_c / total_c) * 100, 1) if total_c > 0 else 0.0
        jobs.append({
            "id": r[0],
            "actor_name": r[1],
            "actor_tag": r[2],
            "target_label": r[3],
            "quiz_title": r[4] or "",
            "total_count": total_c,
            "sent_count": sent_c,
            "failed_count": failed_c,
            "failed_question_ids": f_ids,
            "last_error": r[9] or "",
            "success_rate": rate,
            "status": r[10],
            "created_at": r[11].isoformat() if r[11] else None,
            "completed_at": r[12].isoformat() if r[12] else None,
        })
    return jobs


@app.get("/api/publish_jobs/{job_id}")
@app.get("/publish_jobs/{job_id}")
async def get_single_publish_job(job_id: int, x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT id, actor_id, actor_name, actor_tag, target_label, quiz_title,
                          total_count, sent_count, failed_count, failed_question_ids,
                          last_error, status, created_at, completed_at
                   FROM publish_jobs WHERE id = %s""",
                (job_id,),
            )
            r = c.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="العملية غير موجودة.")

    f_ids = r[9] if isinstance(r[9], list) else (json.loads(r[9]) if r[9] else [])
    total_c = r[6] or 0
    sent_c = r[7] or 0
    failed_c = r[8] or 0
    rate = round((sent_c / total_c) * 100, 1) if total_c > 0 else 0.0
    return {
        "id": r[0],
        "actor_name": r[2],
        "actor_tag": r[3],
        "target_label": r[4],
        "quiz_title": r[5] or "",
        "total_count": total_c,
        "sent_count": sent_c,
        "failed_count": failed_c,
        "failed_question_ids": f_ids,
        "last_error": r[10] or "",
        "success_rate": rate,
        "status": r[11],
        "created_at": r[12].isoformat() if r[12] else None,
        "completed_at": r[13].isoformat() if r[13] else None,
    }


@app.post("/api/publish_jobs/control")
@app.post("/publish_jobs/control")
async def control_publish_job(req: JobControlReq, x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    return {"status": "ok", "job_id": req.job_id, "job_status": "completed"}


@app.get("/api/admins")
@app.get("/admins")
async def list_admins(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "إدارة المشرفين غير متاحة لصلاحياتك الحالية.")
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
    require_perm(actor, "can_manage_admins", "لا تملك صلاحية إدارة المشرفين.")
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
@app.delete("/api/admins/{admin_id}")
async def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "لا تملك صلاحية إزالة المشرفين.")
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
                c.execute("""
                    SELECT id, actor_name, actor_role, actor_tag, action_type, description, payload, undone, created_at
                    FROM audit_logs ORDER BY id DESC LIMIT 120
                """)
            else:
                c.execute("""
                    SELECT id, actor_name, actor_role, actor_tag, action_type, description, payload, undone, created_at
                    FROM audit_logs WHERE actor_id = %s ORDER BY id DESC LIMIT 80
                """, (actor["user_id"],))
            rows = c.fetchall()

    items = []
    for r in rows:
        pl = r[6] if isinstance(r[6], dict) else (json.loads(r[6]) if r[6] else None)
        can_undo = bool(pl and pl.get("undo_kind") == "restore_bank" and not r[7])
        items.append({
            "id": r[0],
            "actor_name": r[1],
            "actor_role": r[2],
            "actor_tag": r[3] or ("المالك" if r[2] == "owner" else "مشرف"),
            "action_type": r[4],
            "description": r[5],
            "can_undo": can_undo,
            "undone": bool(r[7]),
            "created_at": r[8].isoformat() if r[8] else None,
        })
    return items


@app.delete("/api/logs")
@app.delete("/logs")
async def clear_logs(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    if actor["role"] != "owner" and not actor.get("can_manage_admins", False):
        raise HTTPException(status_code=403, detail="مسح السجل غير متاح لصلاحياتك الحالية.")
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("TRUNCATE TABLE audit_logs RESTART IDENTITY")
        conn.commit()
    return {"status": "ok"}


@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def catch_all(full_path: str, request: Request):
    if "setup_webhook" in full_path:
        return await setup_webhook(request)
    if "webhook" in full_path and request.method == "POST":
        return await telegram_webhook(request)
    raise HTTPException(status_code=404, detail=f"المسار غير موجود: /{full_path}")
