import os
import io
import re
import csv
import json
import hmac
import hashlib
import urllib.parse
import asyncio
import random
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
import psycopg2
from psycopg2 import pool
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes


def extract_pdf_mcq_rows(pdf_bytes: bytes) -> Dict[str, Any]:
    """Load the PDF parser only when a PDF import is requested."""
    try:
        from api.pdf_mcq_importer import extract_pdf_mcq_rows as _extract
    except Exception as exc:
        raise ValueError("وحدة استيراد PDF غير متوفرة حالياً على السيرفر.") from exc
    return _extract(pdf_bytes)


def build_bank_pdf_bytes(
    bank_name: str,
    questions: list,
    solved: bool = False,
    settings: Optional[dict] = None,
) -> bytes:
    """Load PyMuPDF/export code only when a PDF export is requested."""
    try:
        from api.pdf_bank_exporter import build_bank_pdf_bytes as _build
    except Exception as exc:
        raise RuntimeError("وحدة تصدير PDF غير متوفرة حالياً على السيرفر.") from exc
    return _build(bank_name, questions, solved=solved, settings=settings)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="Quiz Management & Governance System")


@app.middleware("http")
async def add_server_timing(request: Request, call_next):
    started_at = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.2f}"
    return response


db_pool: Optional[pool.ThreadedConnectionPool] = None
_db_initialized = False
_cleanup_last_run = 0.0
_cleanup_lock = threading.Lock()
_pool_lock = threading.Lock()
_db_init_lock = threading.Lock()
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


def _clean_db_url(url: str) -> str:
    return url.replace("&channel_binding=require", "").replace("?channel_binding=require", "")


def get_pool() -> pool.ThreadedConnectionPool:
    global db_pool
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="متغير DATABASE_URL غير مضاف في إعدادات البيئة.")
    if db_pool is None:
        with _pool_lock:
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
    def __init__(self, readonly: bool = False):
        self.conn = None
        self._pool = None
        self._readonly = readonly

    def __enter__(self):
        self._pool = get_pool()
        self.conn = self._pool.getconn()

        if self.conn.closed:
            self._pool.putconn(self.conn, close=True)
            self.conn = self._pool.getconn()
        if self._readonly and not self.conn.autocommit:
            self.conn.autocommit = True
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is None:
            return

        try:
            if self.conn.closed:
                self._pool.putconn(self.conn, close=True)
                return

            if self._readonly:
                if self.conn.autocommit:
                    self.conn.autocommit = False
            elif exc_type is not None or self.conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                self.conn.rollback()

            self._pool.putconn(self.conn)
        except Exception:
            try:
                self._pool.putconn(self.conn, close=True)
            except Exception:
                pass


def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
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
                        options_json JSONB DEFAULT NULL,
                        correct_idx INTEGER,
                        explanation TEXT DEFAULT ''
                    )
                """)
                c.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS options_json JSONB DEFAULT NULL")

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
                        delay_sec REAL DEFAULT 0.0,
                        shuffle_options BOOLEAN DEFAULT FALSE,
                        open_period INTEGER DEFAULT NULL,
                        status TEXT DEFAULT 'queued',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        completed_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS message_templates (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        category TEXT DEFAULT 'عام',
                        body_html TEXT NOT NULL DEFAULT '',
                        buttons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        disable_preview BOOLEAN DEFAULT TRUE,
                        created_by BIGINT,
                        owner_name TEXT DEFAULT 'المالك',
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank_id ON questions(bank_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_id_id ON audit_logs(actor_id, id DESC)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_publish_jobs_actor ON publish_jobs(actor_id, id DESC)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_publish_jobs_created_at ON publish_jobs(created_at)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_templates_id ON message_templates(id DESC)")
            conn.commit()
        _db_initialized = True


@app.on_event("startup")
def on_startup():
    try:
        init_db()
        cleanup_old_logs()
    except Exception:
        pass


def cleanup_old_logs(force: bool = False):
    global _cleanup_last_run
    now = time.monotonic()
    if not force and now - _cleanup_last_run < CLEANUP_INTERVAL_SECONDS:
        return

    with _cleanup_lock:
        now = time.monotonic()
        if not force and now - _cleanup_last_run < CLEANUP_INTERVAL_SECONDS:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        with PooledConn() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM audit_logs WHERE created_at < %s", (cutoff,))
                c.execute("DELETE FROM publish_jobs WHERE created_at < %s", (cutoff,))
            conn.commit()
        _cleanup_last_run = time.monotonic()


def normalize_options_list(
    options: Optional[List[str]] = None,
    opt1: str = "",
    opt2: str = "",
    opt3: str = "",
    opt4: str = "",
) -> List[str]:
    if options and isinstance(options, list):
        cleaned = [str(x).strip() for x in options if x is not None and str(x).strip() != ""]
    else:
        raw = [opt1, opt2, opt3, opt4]
        cleaned = [str(x).strip() for x in raw if x is not None and str(x).strip() != "" and str(x).strip() != "-"]
    return cleaned[:10]


def unpack_row_options(row_tuple) -> List[str]:
    o1, o2, o3, o4, opts_json = row_tuple
    if opts_json:
        parsed = opts_json if isinstance(opts_json, list) else json.loads(opts_json)
        cleaned = [str(x).strip() for x in parsed if str(x).strip()]
        if len(cleaned) >= 2:
            return cleaned[:10]
    fallback = [str(x).strip() for x in (o1, o2, o3, o4) if x is not None and str(x).strip() not in ("", "-")]
    if len(fallback) >= 2:
        return fallback
    return [str(o1 or "-"), str(o2 or "-")]


def legacy_four_cols(opts: List[str]):
    padded = list(opts[:4])
    while len(padded) < 4:
        padded.append("-")
    return padded[0], padded[1], padded[2], padded[3]


def get_owner_id() -> Optional[int]:
    with PooledConn(readonly=True) as conn:
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


def get_actor_profile(
    user_id: int,
    fallback_name: str = "مستخدم",
    owner_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    owner = get_owner_id() if owner_id is None else owner_id
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
    with PooledConn(readonly=True) as conn:
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
    cleanup_old_logs()
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
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="متغير BOT_TOKEN غير مضاف في إعدادات البيئة.")
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

    user_id = int(user_id)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM config WHERE key = 'owner_id'")
            owner_row = c.fetchone()
            owner_id = int(owner_row[0]) if owner_row else None

            if owner_id is None:
                c.execute(
                    """INSERT INTO config (key, value)
                       VALUES ('owner_id', %s)
                       ON CONFLICT (key) DO NOTHING""",
                    (str(user_id),),
                )
                c.execute("SELECT value FROM config WHERE key = 'owner_id'")
                owner_row = c.fetchone()
                owner_id = int(owner_row[0]) if owner_row else None

                if owner_id == user_id:
                    c.execute(
                        """INSERT INTO admins
                           (user_id, name, tag, can_create_bank, can_edit_others_banks,
                            can_delete, can_publish, can_manage_admins)
                           VALUES (%s, %s, 'المالك', TRUE, TRUE, TRUE, TRUE, TRUE)
                           ON CONFLICT (user_id) DO UPDATE SET
                             name = EXCLUDED.name, tag = 'المالك',
                             can_create_bank = TRUE, can_edit_others_banks = TRUE,
                             can_delete = TRUE, can_publish = TRUE,
                             can_manage_admins = TRUE""",
                        (user_id, display_name),
                    )
                    conn.commit()

            if owner_id == user_id:
                return {
                    "user_id": user_id,
                    "name": display_name,
                    "role": "owner",
                    "role_label": "المالك",
                    "tag": "المالك",
                    "can_create_bank": True,
                    "can_edit_others_banks": True,
                    "can_delete": True,
                    "can_publish": True,
                    "can_manage_admins": True,
                }

            c.execute(
                """SELECT user_id, name, tag, can_create_bank, can_edit_others_banks,
                          can_delete, can_publish, can_manage_admins
                   FROM admins WHERE user_id = %s""",
                (user_id,),
            )
            profile_row = c.fetchone()

    if not profile_row:
        raise HTTPException(status_code=403, detail="عذراً، أنت لست مشرفاً في البوت.")

    return {
        "user_id": profile_row[0],
        "name": profile_row[1] or display_name,
        "role": "admin",
        "role_label": "مشرف",
        "tag": profile_row[2] or "مشرف",
        "can_create_bank": bool(profile_row[3]),
        "can_edit_others_banks": bool(profile_row[4]),
        "can_delete": bool(profile_row[5]),
        "can_publish": bool(profile_row[6]),
        "can_manage_admins": bool(profile_row[7]),
    }


def require_perm(actor: Dict[str, Any], perm_key: str, error_msg: str):
    if actor.get("role") == "owner":
        return
    if not actor.get(perm_key, False):
        raise HTTPException(status_code=403, detail=error_msg)


def get_bank_record(bank_id: int) -> Optional[Dict[str, Any]]:
    with PooledConn(readonly=True) as conn:
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


def _insert_questions_into_bank(
    actor: Dict[str, Any],
    rows_to_insert: list,
    bank_name: str = "",
    bank_id: Optional[int] = None,
) -> Dict[str, Any]:
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
                    clean_bank = (bank_name or "").strip()
                    if not clean_bank:
                        raise HTTPException(status_code=400, detail="يرجى إدخال اسم البنك.")
                    c.execute(
                        "INSERT INTO banks (name, owner_id, owner_name) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
                        (clean_bank, actor["user_id"], actor_label),
                    )
                    c.execute("SELECT id, name FROM banks WHERE name = %s", (clean_bank,))
                    b_row = c.fetchone()
                    target_bank_id, target_bank_name = b_row[0], b_row[1]

                for item in rows_to_insert:
                    if len(item) == 4:
                        q_text, opts_list, c_idx, expl = item
                    else:
                        q_text, o1, o2, o3, o4, c_idx, expl = item
                        opts_list = normalize_options_list(opt1=o1, opt2=o2, opt3=o3, opt4=o4)
                    o1, o2, o3, o4 = legacy_four_cols(opts_list)
                    c.execute(
                        """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation)
                           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                        (
                            target_bank_id,
                            q_text,
                            o1,
                            o2,
                            o3,
                            o4,
                            json.dumps(opts_list, ensure_ascii=False),
                            c_idx,
                            expl,
                        ),
                    )
            conn.commit()
            return {"bank_id": target_bank_id, "bank_name": target_bank_name, "added": len(rows_to_insert)}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=f"تعذر حفظ الأسئلة: {str(e)}")


def get_base_url(request: Request) -> str:
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"https://{host}".rstrip("/")


@app.get("/", response_class=HTMLResponse)
def serve_home():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "public", "index.html"),
        os.path.join(here, "..", "public", "index.html"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>App Ready! Open /api/setup_webhook</h1>"


class QuestionUpdate(BaseModel):
    question: str
    options: Optional[List[str]] = None
    opt1: Optional[str] = ""
    opt2: Optional[str] = ""
    opt3: Optional[str] = ""
    opt4: Optional[str] = ""
    correct_idx: int
    explanation: Optional[str] = ""


class QuestionCreate(QuestionUpdate):
    bank_id: int


class TransferQuestionReq(BaseModel):
    target_bank_id: int


class BankRenameReq(BaseModel):
    name: str


class ExportCsvReq(BaseModel):
    bank_id: int
    question_ids: Optional[List[int]] = None


class ExportPdfReq(BaseModel):
    bank_id: int
    question_ids: Optional[List[int]] = None
    ordered_question_ids: Optional[List[int]] = None
    shuffle_questions: bool = False
    settings: Optional[Dict[str, Any]] = None


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


class RetryFailedReq(BaseModel):
    job_id: int


class AdminUpsertReq(BaseModel):
    user_id: int
    name: Optional[str] = "مشرف"
    tag: Optional[str] = "مشرف"
    can_create_bank: bool = True
    can_edit_others_banks: bool = False
    can_delete: bool = False
    can_publish: bool = True
    can_manage_admins: bool = False


class BroadcastButtonItem(BaseModel):
    text: str
    url: str


class BroadcastSendReq(BaseModel):
    target_type: str = "saved_comm"
    comm_id: Optional[int] = None
    body_html: str
    buttons: List[List[BroadcastButtonItem]] = []
    disable_preview: bool = True
    silent: bool = False
    pin_message: bool = False
    template_name: Optional[str] = None


class TemplateUpsertReq(BaseModel):
    name: str
    category: Optional[str] = "عام"
    body_html: str = ""
    buttons: List[List[BroadcastButtonItem]] = []
    disable_preview: bool = True


def _sanitize_button_url(raw_url: str) -> str:
    clean = (raw_url or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="كل زر رابط يجب أن يحتوي على رابط صالح.")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme.lower() not in ("http", "https", "tg"):
        raise HTTPException(
            status_code=400,
            detail=f"الرابط «{clean}» غير مدعوم. يُسمح فقط بروابط https:// أو http:// أو tg://",
        )
    return clean


def _build_url_keyboard(buttons_grid: List[List[BroadcastButtonItem]]) -> Optional[InlineKeyboardMarkup]:
    rows: List[List[InlineKeyboardButton]] = []
    for row in (buttons_grid or [])[:12]:
        kb_row: List[InlineKeyboardButton] = []
        for btn in (row or [])[:4]:
            label = (btn.text or "").strip()
            if not label:
                continue
            safe_url = _sanitize_button_url(btn.url)
            kb_row.append(InlineKeyboardButton(text=label[:64], url=safe_url))
        if kb_row:
            rows.append(kb_row)
    return InlineKeyboardMarkup(rows) if rows else None


def _normalize_buttons_payload(buttons_grid: List[List[BroadcastButtonItem]]) -> List[List[Dict[str, str]]]:
    normalized: List[List[Dict[str, str]]] = []
    for row in (buttons_grid or [])[:12]:
        clean_row = []
        for btn in (row or [])[:4]:
            label = (btn.text or "").strip()
            if not label:
                continue
            safe_url = _sanitize_button_url(btn.url)
            clean_row.append({"text": label[:64], "url": safe_url})
        if clean_row:
            normalized.append(clean_row)
    return normalized


def build_bot_app(webapp_url: str = ""):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not configured.")
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

    async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not msg or not chat:
            return

        chat_title = (getattr(chat, "title", None) or "").strip()
        lines = []
        if chat.type in ("group", "supergroup", "channel"):
            lines.append(f"اسم المجموعة / القناة: <b>{chat_title or 'بدون اسم'}</b>")
            lines.append(f"معرّف المجموعة / القناة (Chat ID): <code>{chat.id}</code>")
            topic_id = getattr(msg, "message_thread_id", None)
            if topic_id:
                lines.append(f"رقم الموضوع (Topic ID): <code>{topic_id}</code>")
            else:
                lines.append("رقم الموضوع (Topic ID): <i>لا يوجد موضوع (رسالة عامة)</i>")
        else:
            lines.append(f"معرّف المحادثة (Chat ID): <code>{chat.id}</code>")

        if user:
            lines.append(f"معرّف المستخدم (User ID): <code>{user.id}</code>")

        reply_kwargs = {
            "text": "\n".join(lines),
            "parse_mode": ParseMode.HTML,
        }
        if getattr(msg, "message_thread_id", None):
            reply_kwargs["message_thread_id"] = msg.message_thread_id

        await msg.reply_text(**reply_kwargs)

    async def handle_pdf_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
        init_db()
        doc = update.message.document if update.message else None
        if not doc:
            return
        fname = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if not fname.endswith(".pdf") and "pdf" not in mime:
            return

        user = update.effective_user
        display_name = format_display_name({
            "first_name": getattr(user, "first_name", ""),
            "last_name": getattr(user, "last_name", ""),
            "username": getattr(user, "username", ""),
        })
        actor = get_actor_profile(user.id, display_name)
        if not actor or (actor["role"] != "owner" and not actor["can_create_bank"]):
            await update.message.reply_text("عذراً، لا تملك صلاحية استيراد بنوك أسئلة من PDF.")
            return

        tg_file = await doc.get_file()
        pdf_bytes = bytes(await tg_file.download_as_bytearray())

        try:
            parsed = extract_pdf_mcq_rows(pdf_bytes)
            base_name = os.path.splitext(doc.file_name or "بنك PDF")[0].strip() or "بنك PDF"
            saved = _insert_questions_into_bank(actor, parsed["csv_rows"], bank_name=base_name)
            write_audit_log(
                actor,
                "import_pdf_bot",
                f"استورد {parsed['total_extracted']} سؤال MCQ من PDF عبر البوت إلى «{saved['bank_name']}» (مراجعة: {parsed['needs_review_count']}، مقالي معزول: {parsed['essay_count']}).",
            )
            await update.message.reply_text(
                f"تم حفظ {parsed['total_extracted']} سؤال MCQ في البنك «{saved['bank_name']}».\n"
                f"المؤكدة تلقائياً: {parsed['verified_count']} | تحتاج مراجعة: {parsed['needs_review_count']}\n"
                f"الأسئلة المقالية المعزولة: {parsed['essay_count']}"
            )
        except Exception as e:
            await update.message.reply_text(f"تعذر استخراج الأسئلة: {e}")

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_doc))
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
            menu_button=MenuButtonWebApp(text="إدارة الاختبارات", web_app=WebAppInfo(url=base_url))
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
def get_me(x_telegram_init_data: str = Header(default="")):
    return verify_actor(x_telegram_init_data)


@app.get("/api/banks")
@app.get("/banks")
def get_banks(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
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
def create_bank(req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
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
def rename_bank(bank_id: int, req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
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
def delete_bank(bank_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "لا تملك صلاحية الحذف.")
    bank = get_bank_record(bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC",
                (bank_id,),
            )
            q_rows = c.fetchall()
            snapshot_questions = []
            for r in q_rows:
                opts_list = unpack_row_options((r[1], r[2], r[3], r[4], r[5]))
                snapshot_questions.append({
                    "question": r[0],
                    "options": opts_list,
                    "opt1": r[1],
                    "opt2": r[2],
                    "opt3": r[3],
                    "opt4": r[4],
                    "correct_idx": r[6],
                    "explanation": r[7] or "",
                })
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


@app.post("/api/export_csv")
@app.post("/export_csv")
async def export_questions_csv(
    req: ExportCsvReq,
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    bank = get_bank_record(req.bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")

    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            if req.question_ids:
                c.execute(
                    """SELECT id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation
                       FROM questions WHERE bank_id = %s AND id = ANY(%s)""",
                    (req.bank_id, req.question_ids),
                )
                q_map = {r[0]: r for r in c.fetchall()}
                rows = [q_map[qid] for qid in req.question_ids if qid in q_map]
            else:
                c.execute(
                    """SELECT id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation
                       FROM questions WHERE bank_id = %s ORDER BY id ASC""",
                    (req.bank_id,),
                )
                rows = c.fetchall()

    if not rows:
        raise HTTPException(status_code=400, detail="لا توجد أسئلة محددة للتصدير.")

    unpacked_rows = []
    max_opts_count = 4
    for r in rows:
        _, q_text, o1, o2, o3, o4, opts_json, c_idx, expl = r
        opts = unpack_row_options((o1, o2, o3, o4, opts_json))
        max_opts_count = max(max_opts_count, len(opts))
        ans_text = opts[c_idx] if isinstance(c_idx, int) and 0 <= c_idx < len(opts) else ""
        unpacked_rows.append((q_text or "", opts, ans_text, expl or ""))

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Question"] + [f"Option {i + 1}" for i in range(max_opts_count)] + ["Correct Answer", "Explanation"]
    writer.writerow(header)

    for q_text, opts, ans_text, expl in unpacked_rows:
        padded_opts = list(opts)
        while len(padded_opts) < max_opts_count:
            padded_opts.append("-")
        writer.writerow([q_text, *padded_opts[:max_opts_count], ans_text, expl])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    safe_slug = re.sub(r"[^\w\u0600-\u06FF-]+", "_", bank["name"]).strip("_") or "bank_questions"
    filename = f"{safe_slug}.csv"

    bot_app = build_bot_app()
    async with bot_app:
        try:
            await bot_app.bot.send_document(
                chat_id=actor["user_id"],
                document=io.BytesIO(csv_bytes),
                filename=filename,
                caption=f"ملف CSV للبنك «{bank['name']}» ({len(rows)} سؤال).",
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"تعذر إرسال ملف CSV إلى محادثتك الخاصة: تأكد من بدء محادثة مع البوت أولاً. ({e})",
            )

    write_audit_log(
        actor,
        "export_csv",
        f"صدّر {len(rows)} سؤالاً كملف CSV إلى الخاص من بنك «{bank['name']}».",
    )
    return {"status": "ok", "exported_count": len(rows), "filename": filename}


@app.post("/api/export_pdf_pair")
@app.post("/export_pdf_pair")
async def export_bank_pdf_pair(
    req: ExportPdfReq,
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    bank = get_bank_record(req.bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")

    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation
                   FROM questions WHERE bank_id = %s ORDER BY id ASC""",
                (req.bank_id,),
            )
            all_rows = c.fetchall()

    if not all_rows:
        raise HTTPException(status_code=400, detail="البنك لا يحتوي على أسئلة.")

    q_dict: Dict[int, Dict[str, Any]] = {}
    default_order: List[int] = []
    for r in all_rows:
        qid = r[0]
        default_order.append(qid)
        opts = unpack_row_options((r[2], r[3], r[4], r[5], r[6]))
        c_idx = r[7] if isinstance(r[7], int) and 0 <= r[7] < len(opts) else 0
        q_dict[qid] = {
            "id": qid,
            "question": r[1],
            "options": opts,
            "opt1": opts[0] if len(opts) > 0 else "-",
            "opt2": opts[1] if len(opts) > 1 else "-",
            "opt3": opts[2] if len(opts) > 2 else "-",
            "opt4": opts[3] if len(opts) > 3 else "-",
            "correct_idx": c_idx,
            "explanation": r[8] or "",
        }

    selected_set = set(req.question_ids) if req.question_ids is not None else set(default_order)
    order_seq = req.ordered_question_ids if req.ordered_question_ids else default_order

    seen = set()
    final_ids: List[int] = []
    for qid in order_seq:
        if qid in q_dict and qid in selected_set and qid not in seen:
            seen.add(qid)
            final_ids.append(qid)
    for qid in default_order:
        if qid in selected_set and qid not in seen:
            seen.add(qid)
            final_ids.append(qid)

    if not final_ids:
        raise HTTPException(status_code=400, detail="يرجى تحديد سؤال واحد على الأقل لتصدير PDF.")

    selected_questions = [q_dict[qid] for qid in final_ids]
    if req.shuffle_questions:
        random.shuffle(selected_questions)

    cfg = req.settings or {}
    try:
        unsolved_bytes = build_bank_pdf_bytes(
            bank_name=bank["name"],
            questions=selected_questions,
            solved=False,
            settings=cfg,
        )
        solved_bytes = build_bank_pdf_bytes(
            bank_name=bank["name"],
            questions=selected_questions,
            solved=True,
            settings=cfg,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر توليد ملفات PDF: {str(e)}")

    safe_slug = re.sub(r"[^\w\u0600-\u06FF-]+", "_", bank["name"]).strip("_") or "bank_questions"
    unsolved_name = f"{safe_slug}_Unsolved.pdf"
    solved_name = f"{safe_slug}_Solved.pdf"

    bot_app = build_bot_app()
    async with bot_app:
        try:
            await bot_app.bot.send_document(
                chat_id=actor["user_id"],
                document=io.BytesIO(solved_bytes),
                filename=solved_name,
                caption=f"النسخة المحلولة (Solved) — {bank['name']} ({len(selected_questions)} سؤال).",
            )
            await bot_app.bot.send_document(
                chat_id=actor["user_id"],
                document=io.BytesIO(unsolved_bytes),
                filename=unsolved_name,
                caption=f"النسخة غير المحلولة (Unsolved) — {bank['name']} ({len(selected_questions)} سؤال).",
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"تعذر إرسال ملفي PDF إلى محادثتك الخاصة: تأكد من بدء محادثة مع البوت أولاً. ({e})",
            )

    write_audit_log(
        actor,
        "export_pdf_pair",
        f"صدّر نسختي PDF (محلولة وغير محلولة) لـ {len(selected_questions)} سؤال من بنك «{bank['name']}» إلى الخاص.",
    )
    return {
        "status": "ok",
        "exported_count": len(selected_questions),
        "solved_file": solved_name,
        "unsolved_file": unsolved_name,
    }


@app.post("/api/logs/{log_id}/undo")
@app.post("/logs/{log_id}/undo")
def undo_audit_log(log_id: int, x_telegram_init_data: str = Header(default="")):
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
                opts = normalize_options_list(
                    options=q.get("options"),
                    opt1=q.get("opt1", ""),
                    opt2=q.get("opt2", ""),
                    opt3=q.get("opt3", ""),
                    opt4=q.get("opt4", ""),
                )
                o1, o2, o3, o4 = legacy_four_cols(opts)
                c.execute(
                    """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                    (
                        new_bank_id,
                        q["question"],
                        o1,
                        o2,
                        o3,
                        o4,
                        json.dumps(opts, ensure_ascii=False),
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
    if ans_raw_val is None:
        return None
    s = str(ans_raw_val).strip()
    if not s or s.lower() == "nan":
        return None

    s_norm = " ".join(s.split()).lower()
    for i, opt in enumerate(opts):
        opt_norm = " ".join(str(opt).split()).lower()
        if s_norm == opt_norm and opt_norm != "-":
            return i

    if re.fullmatch(r"\d+(\.0+)?", s):
        num = int(float(s))
        if 1 <= num <= len(opts):
            return num - 1

    s_clean = s.upper().rstrip(".").rstrip(")").strip()
    letter_map = {
        "A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6, "H": 7, "I": 8, "J": 9,
        "أ": 0, "ا": 0, "ب": 1, "ج": 2, "د": 3, "هـ": 4, "ه": 4, "و": 5, "ز": 6, "ح": 7,
    }
    if s_clean in letter_map and letter_map[s_clean] < len(opts):
        return letter_map[s_clean]

    return None


@app.post("/api/upload_pdf")
@app.post("/upload_pdf")
def upload_pdf(
    bank_name: str = Form(default=""),
    bank_id: Optional[int] = Form(default=None),
    file: UploadFile = File(...),
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة PDF.")

    pdf_bytes = file.file.read()
    try:
        parsed = extract_pdf_mcq_rows(pdf_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر تحليل ملف PDF: {str(e)}")

    saved = _insert_questions_into_bank(
        actor,
        parsed["csv_rows"],
        bank_name=bank_name,
        bank_id=bank_id,
    )
    write_audit_log(
        actor,
        "import_pdf",
        f"استورد {parsed['total_extracted']} سؤال MCQ من PDF إلى بنك «{saved['bank_name']}» (مراجعة: {parsed['needs_review_count']}، مقالي معزول: {parsed['essay_count']}).",
    )
    return {
        "status": "ok",
        "added": parsed["total_extracted"],
        "verified_count": parsed["verified_count"],
        "needs_review_count": parsed["needs_review_count"],
        "essay_count": parsed["essay_count"],
        "essay_questions": parsed["essay_questions"],
        "review_items": parsed["review_items"],
        "scanned_pages": parsed["scanned_pages"],
        "bank_id": saved["bank_id"],
    }


@app.post("/api/upload_csv")
@app.post("/upload_csv")
def upload_csv(
    bank_name: str = Form(default=""),
    bank_id: Optional[int] = Form(default=None),
    structure: str = Form(default="auto"),
    file: UploadFile = File(...),
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    content = file.file.read()
    import pandas as pd

    df = None
    for enc in ["utf-8-sig", "utf-8", "cp1256", "windows-1256", "latin1"]:
        try:
            df = pd.read_csv(io.BytesIO(content), header=None, encoding=enc, sep=None, engine="python")
            break
        except Exception:
            continue
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail="تعذر قراءة ملف CSV أو الملف فارغ.")

    if df.shape[1] < 4:
        raise HTTPException(status_code=400, detail=f"الملف يحتوي على {df.shape[1]} أعمدة فقط؛ المطلوب 4 أعمدة على الأقل.")

    mode = (structure or "auto").strip().lower()

    def _extract_csv_row(row_vals):
        non_empty = [
            str(val).strip()
            for val in row_vals
            if not pd.isna(val) and str(val).strip() != ""
        ]
        if len(non_empty) < 4:
            return None

        if mode == "q_opts_expl_ans":
            if len(non_empty) < 5:
                return "INVALID_ANSWER"
            q_text = non_empty[0]
            ans_val = non_empty[-1]
            expl_val = non_empty[-2]
            candidate_opts = [x for x in non_empty[1:-2] if x != "-"]
            if not (2 <= len(candidate_opts) <= 10):
                return "INVALID_ANSWER"
            c_idx = parse_correct_index(ans_val, candidate_opts)
            if c_idx is None:
                return "INVALID_ANSWER"
            return (q_text, candidate_opts, c_idx, expl_val)

        if mode == "num_q_opts_ans_expl":
            if len(non_empty) < 5:
                return "INVALID_ANSWER"
            q_text = non_empty[1]
            for ans_pos in (len(non_empty) - 1, len(non_empty) - 2):
                if ans_pos >= 4:
                    candidate_opts = [x for x in non_empty[2:ans_pos] if x != "-"]
                    if 2 <= len(candidate_opts) <= 10:
                        c_idx = parse_correct_index(non_empty[ans_pos], candidate_opts)
                        if c_idx is not None:
                            expl_val = non_empty[ans_pos + 1] if ans_pos + 1 < len(non_empty) else ""
                            return (q_text, candidate_opts, c_idx, expl_val)
            return "INVALID_ANSWER"

        q_text = non_empty[0]
        for ans_pos in (len(non_empty) - 1, len(non_empty) - 2):
            if ans_pos >= 3:
                candidate_opts = [x for x in non_empty[1:ans_pos] if x != "-"]
                if 2 <= len(candidate_opts) <= 10:
                    c_idx = parse_correct_index(non_empty[ans_pos], candidate_opts)
                    if c_idx is not None:
                        expl_val = non_empty[ans_pos + 1] if ans_pos + 1 < len(non_empty) else ""
                        return (q_text, candidate_opts, c_idx, expl_val)
        return "INVALID_ANSWER"

    first_check = _extract_csv_row(df.iloc[0].tolist())
    if first_check in (None, "INVALID_ANSWER"):
        df = df.iloc[1:]

    rows_to_insert = []
    for idx, row in df.iterrows():
        if pd.isna(row[0]) or str(row[0]).strip() == "":
            continue
        parsed_row = _extract_csv_row(row.tolist())
        if parsed_row is None:
            continue
        if parsed_row == "INVALID_ANSWER":
            q_preview = str(row[0]).strip()[:30]
            raise HTTPException(
                status_code=400,
                detail=f"خطأ في الصف {idx + 1} (السؤال: {q_preview}...) مع البنية المختارة ({mode}): تأكد من مطابقة ترتيب الأعمدة.",
            )
        rows_to_insert.append(parsed_row)

    saved = _insert_questions_into_bank(
        actor,
        rows_to_insert,
        bank_name=bank_name,
        bank_id=bank_id,
    )
    write_audit_log(
        actor,
        "import_csv",
        f"استورد {len(rows_to_insert)} سؤالاً من CSV (بنية: {mode}) إلى بنك «{saved['bank_name']}».",
    )
    return {"status": "ok", "added": len(rows_to_insert), "bank_id": saved["bank_id"]}


@app.get("/api/banks/{bank_id}/questions")
@app.get("/banks/{bank_id}/questions")
def get_questions(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation
                   FROM questions WHERE bank_id = %s ORDER BY id ASC""",
                (bank_id,),
            )
            rows = c.fetchall()

    result = []
    for r in rows:
        opts = unpack_row_options((r[2], r[3], r[4], r[5], r[6]))
        c_idx = r[7] if isinstance(r[7], int) and 0 <= r[7] < len(opts) else 0
        result.append({
            "id": r[0],
            "question": r[1],
            "options": opts,
            "opt1": opts[0] if len(opts) > 0 else "-",
            "opt2": opts[1] if len(opts) > 1 else "-",
            "opt3": opts[2] if len(opts) > 2 else "-",
            "opt4": opts[3] if len(opts) > 3 else "-",
            "correct_idx": c_idx,
            "explanation": r[8],
        })
    return result


@app.post("/api/questions")
@app.post("/questions")
def add_question(req: QuestionCreate, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    bank = get_bank_record(req.bank_id)
    if not bank:
        raise HTTPException(status_code=404, detail="البنك غير موجود.")
    require_bank_edit_access(actor, bank)

    opts = normalize_options_list(req.options, req.opt1, req.opt2, req.opt3, req.opt4)
    if len(opts) < 2:
        raise HTTPException(status_code=400, detail="يجب إدخال خيارين على الأقل.")
    if not (0 <= req.correct_idx < len(opts)):
        raise HTTPException(status_code=400, detail="رقم الخيار الصحيح غير صالح.")

    o1, o2, o3, o4 = legacy_four_cols(opts)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                (
                    req.bank_id,
                    req.question,
                    o1,
                    o2,
                    o3,
                    o4,
                    json.dumps(opts, ensure_ascii=False),
                    req.correct_idx,
                    req.explanation,
                ),
            )
        conn.commit()

    write_audit_log(actor, "add_question", f"أضاف سؤالاً جديداً إلى بنك «{bank['name']}».")
    return {"status": "ok"}


@app.put("/api/questions/{q_id}")
@app.put("/questions/{q_id}")
def update_question(q_id: int, req: QuestionUpdate, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute("SELECT bank_id FROM questions WHERE id = %s", (q_id,))
            row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="السؤال غير موجود.")
    bank = get_bank_record(row[0])
    if bank:
        require_bank_edit_access(actor, bank)

    opts = normalize_options_list(req.options, req.opt1, req.opt2, req.opt3, req.opt4)
    if len(opts) < 2:
        raise HTTPException(status_code=400, detail="يجب إدخال خيارين على الأقل.")
    if not (0 <= req.correct_idx < len(opts)):
        raise HTTPException(status_code=400, detail="رقم الخيار الصحيح غير صالح.")

    o1, o2, o3, o4 = legacy_four_cols(opts)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE questions
                   SET question=%s, opt1=%s, opt2=%s, opt3=%s, opt4=%s, options_json=%s::jsonb, correct_idx=%s, explanation=%s
                   WHERE id=%s""",
                (
                    req.question,
                    o1,
                    o2,
                    o3,
                    o4,
                    json.dumps(opts, ensure_ascii=False),
                    req.correct_idx,
                    req.explanation,
                    q_id,
                ),
            )
        conn.commit()

    write_audit_log(actor, "edit_question", f"عدّل سؤالاً في بنك «{bank['name'] if bank else q_id}».")
    return {"status": "ok"}


@app.post("/api/questions/{q_id}/move")
@app.post("/questions/{q_id}/move")
def move_question(q_id: int, req: TransferQuestionReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
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
@app.post("/questions/{q_id}/copy")
def copy_question(q_id: int, req: TransferQuestionReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT bank_id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation FROM questions WHERE id = %s",
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

    opts_json_str = json.dumps(unpack_row_options((row[2], row[3], row[4], row[5], row[6])), ensure_ascii=False)
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                (req.target_bank_id, row[1], row[2], row[3], row[4], row[5], opts_json_str, row[7], row[8]),
            )
        conn.commit()

    write_audit_log(
        actor,
        "copy_question",
        f"نسخ سؤالاً من بنك «{src_bank['name'] if src_bank else ''}» إلى بنك «{dst_bank['name']}».",
    )
    return {"status": "ok"}


@app.delete("/api/questions/{q_id}")
@app.delete("/questions/{q_id}")
def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "لا تملك صلاحية حذف الأسئلة.")
    with PooledConn(readonly=True) as conn:
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
def get_communities(x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, title, chat_id, topic_id FROM communities ORDER BY id DESC")
            rows = c.fetchall()
    return [{"id": r[0], "title": r[1], "chat_id": r[2], "topic_id": r[3]} for r in rows]


@app.post("/api/communities")
@app.post("/communities")
def add_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
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
def delete_community(comm_id: int, x_telegram_init_data: str = Header(default="")):
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
    bot_app = build_bot_app()
    async with bot_app:
        try:
            kwargs = {"chat_id": req.chat_id.strip(), "text": "تم التحقق من صلاحيات البوت والاتصال بالوجهة بنجاح."}
            if req.topic_id:
                kwargs["message_thread_id"] = req.topic_id
            await bot_app.bot.send_message(**kwargs)
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"تعذر الإرسال: تأكد من إضافة البوت كمشرف ومنح صلاحية النشر. ({e})",
            )


@app.get("/api/templates")
@app.get("/templates")
def list_templates(x_telegram_init_data: str = Header(default="")):
    verify_actor(x_telegram_init_data)
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, name, category, body_html, buttons_json, disable_preview, created_by, owner_name, updated_at
                FROM message_templates ORDER BY id DESC
            """)
            rows = c.fetchall()

    items = []
    for r in rows:
        btns = r[4] if isinstance(r[4], list) else (json.loads(r[4]) if r[4] else [])
        items.append({
            "id": r[0],
            "name": r[1],
            "category": r[2] or "عام",
            "body_html": r[3] or "",
            "buttons": btns,
            "disable_preview": bool(r[5]),
            "created_by": r[6],
            "owner_name": r[7] or "المالك",
            "updated_at": r[8].isoformat() if r[8] else None,
        })
    return items


@app.post("/api/templates")
@app.post("/templates")
def create_template(req: TemplateUpsertReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "لا تملك صلاحية حفظ وإدارة التيمبلتس.")

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="يرجى إدخال اسم التيمبلت.")
    if not (req.body_html or "").strip() and not req.buttons:
        raise HTTPException(status_code=400, detail="يرجى إدخال نص الرسالة أو إضافة زر رابط واحد على الأقل.")

    clean_btns = _normalize_buttons_payload(req.buttons)
    actor_label = f"{actor['name']} ({actor['tag']})" if actor["role"] == "admin" else "المالك"

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO message_templates (name, category, body_html, buttons_json, disable_preview, created_by, owner_name, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, NOW())
                   RETURNING id""",
                (
                    name,
                    (req.category or "عام").strip() or "عام",
                    req.body_html or "",
                    json.dumps(clean_btns, ensure_ascii=False),
                    bool(req.disable_preview),
                    actor["user_id"],
                    actor_label,
                ),
            )
            tpl_id = c.fetchone()[0]
        conn.commit()

    write_audit_log(actor, "create_template", f"حفظ تيمبلت النشر «{name}».")
    return {"status": "ok", "id": tpl_id, "name": name}


@app.put("/api/templates/{tpl_id}")
@app.put("/templates/{tpl_id}")
def update_template(tpl_id: int, req: TemplateUpsertReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "لا تملك صلاحية تعديل التيمبلتس.")

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="يرجى إدخال اسم التيمبلت.")
    clean_btns = _normalize_buttons_payload(req.buttons)

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id FROM message_templates WHERE id = %s", (tpl_id,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail="التيمبلت غير موجود.")
            c.execute(
                """UPDATE message_templates
                   SET name = %s, category = %s, body_html = %s, buttons_json = %s::jsonb,
                       disable_preview = %s, updated_at = NOW()
                   WHERE id = %s""",
                (
                    name,
                    (req.category or "عام").strip() or "عام",
                    req.body_html or "",
                    json.dumps(clean_btns, ensure_ascii=False),
                    bool(req.disable_preview),
                    tpl_id,
                ),
            )
        conn.commit()

    write_audit_log(actor, "update_template", f"حدّث تيمبلت النشر «{name}».")
    return {"status": "ok", "id": tpl_id}


@app.delete("/api/templates/{tpl_id}")
@app.delete("/templates/{tpl_id}")
def delete_template(tpl_id: int, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_delete", "لا تملك صلاحية حذف التيمبلتس.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT name FROM message_templates WHERE id = %s", (tpl_id,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="التيمبلت غير موجود.")
            c.execute("DELETE FROM message_templates WHERE id = %s", (tpl_id,))
        conn.commit()

    write_audit_log(actor, "delete_template", f"حذف تيمبلت النشر «{row[0]}».")
    return {"status": "ok"}


@app.post("/api/broadcast_message")
@app.post("/broadcast_message")
async def broadcast_message(req: BroadcastSendReq, x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "لا تملك صلاحية إرسال الرسائل إلى الوجهات.")

    body_text = (req.body_html or "").strip()
    if not body_text:
        raise HTTPException(status_code=400, detail="يرجى كتابة نص الرسالة قبل الإرسال.")
    if len(body_text) > 4000:
        raise HTTPException(status_code=400, detail="نص الرسالة يتجاوز الحد المسموح به في تيليجرام (4000 حرف).")

    markup = _build_url_keyboard(req.buttons)
    topic_id: Optional[int] = None

    if req.target_type == "private":
        target_chat: Any = actor["user_id"]
        target_label = "المحادثة الخاصة"
    elif req.target_type == "saved_comm":
        if not req.comm_id:
            raise HTTPException(status_code=400, detail="يرجى اختيار الوجهة المستهدفة.")
        with PooledConn(readonly=True) as conn:
            with conn.cursor() as c:
                c.execute("SELECT title, chat_id, topic_id FROM communities WHERE id = %s", (req.comm_id,))
                comm_row = c.fetchone()
        if not comm_row:
            raise HTTPException(status_code=404, detail="الوجهة المحددة غير موجودة.")
        target_label = comm_row[0]
        target_chat = comm_row[1].strip()
        topic_id = comm_row[2]
    else:
        raise HTTPException(status_code=400, detail="نوع الوجهة غير صالح.")

    send_kwargs: Dict[str, Any] = {
        "chat_id": target_chat,
        "text": body_text,
        "parse_mode": ParseMode.HTML,
        "disable_web_page_preview": bool(req.disable_preview),
        "disable_notification": bool(req.silent),
    }
    if topic_id:
        send_kwargs["message_thread_id"] = topic_id
    if markup:
        send_kwargs["reply_markup"] = markup

    bot_app = build_bot_app()
    async with bot_app:
        try:
            sent_msg = await bot_app.bot.send_message(**send_kwargs)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"تعذر إرسال الرسالة: تأكد من صحة تنسيق HTML وصلاحيات البوت في الوجهة. ({e})",
            )

        pinned_ok = False
        if req.pin_message and sent_msg and getattr(sent_msg, "message_id", None):
            try:
                await bot_app.bot.pin_chat_message(
                    chat_id=target_chat,
                    message_id=sent_msg.message_id,
                    disable_notification=True,
                )
                pinned_ok = True
            except Exception:
                pinned_ok = False

    tpl_note = f" (عبر تيمبلت: {req.template_name})" if req.template_name else ""
    pin_note = " • مثبتة" if pinned_ok else ""
    write_audit_log(
        actor,
        "broadcast_message",
        f"أرسل رسالة منسقة بأزرار روابط إلى «{target_label}»{tpl_note}{pin_note}.",
    )
    return {
        "status": "ok",
        "target_label": target_label,
        "pinned": pinned_ok,
    }


def _load_publish_job_rows(job_id: int, q_ids: List[int]):
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE publish_jobs SET status = 'running' WHERE id = %s", (job_id,))
            c.execute(
                """SELECT id, question, opt1, opt2, opt3, opt4, options_json, correct_idx, explanation
                   FROM questions WHERE id = ANY(%s)""",
                (q_ids,),
            )
            q_map = {r[0]: r for r in c.fetchall()}
        conn.commit()
    return q_map


def _persist_publish_progress(job_id: int, sent_count: int, failed_count: int, failed_ids: List[int]):
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE publish_jobs
                   SET sent_count = %s, failed_count = %s, failed_question_ids = %s::jsonb
                   WHERE id = %s""",
                (sent_count, failed_count, json.dumps(failed_ids), job_id),
            )
        conn.commit()


def _finalize_publish_job(job_id: int, status: str, sent_count: int, failed_count: int, failed_ids: List[int]):
    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE publish_jobs
                   SET status = %s, sent_count = %s, failed_count = %s,
                       failed_question_ids = %s::jsonb, completed_at = NOW()
                   WHERE id = %s""",
                (status, sent_count, failed_count, json.dumps(failed_ids), job_id),
            )
        conn.commit()


async def execute_publish_job(
    job_id: int,
    q_ids: List[int],
    target_chat,
    target_label: str,
    topic_id: Optional[int],
    delay_sec: float,
    shuffle_options: bool,
    open_period: Optional[int],
    quiz_title: Optional[str],
    actor: Dict[str, Any],
    is_retry: bool = False,
):
    q_map = await run_in_threadpool(_load_publish_job_rows, job_id, q_ids)
    ordered_rows = [q_map[qid] for qid in q_ids if qid in q_map]
    total_requested = len(ordered_rows)
    sent_count = 0
    failed_count = 0
    failed_ids: List[int] = []
    admin_id = actor["user_id"]

    is_group_or_channel = str(target_chat).startswith("-") or str(target_chat).startswith("@")
    bot_app = build_bot_app()
    async with bot_app:
        if quiz_title and quiz_title.strip() and total_requested > 0:
            try:
                prefix = "إعادة إرسال الأسئلة المتعذرة" if is_retry else "اختبار جديد"
                intro_kwargs = {
                    "chat_id": target_chat,
                    "text": f"**{quiz_title.strip()}**\n{prefix} • عدد الأسئلة: **{total_requested}**",
                    "parse_mode": "Markdown",
                }
                if topic_id:
                    intro_kwargs["message_thread_id"] = topic_id
                await bot_app.bot.send_message(**intro_kwargs)
            except Exception:
                pass

        for idx, row in enumerate(ordered_rows):
            qid, q_text, o1, o2, o3, o4, opts_json, c_idx, expl = row
            raw_opts = unpack_row_options((o1, o2, o3, o4, opts_json))
            opts = [x[:98] for x in raw_opts[:10]]
            current_correct = c_idx if isinstance(c_idx, int) and 0 <= c_idx < len(opts) else 0

            if shuffle_options:
                indexed = list(enumerate(opts))
                random.shuffle(indexed)
                opts = [x[1] for x in indexed]
                current_correct = [i for i, x in enumerate(indexed) if x[0] == current_correct][0]

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
            attempts = 0
            while attempts < 4:
                attempts += 1
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
                        break

            if not delivered:
                failed_count += 1
                failed_ids.append(qid)

            checkpoint_due = (
                (idx + 1) % 5 == 0
                or idx == total_requested - 1
                or not delivered
            )
            if checkpoint_due:
                await run_in_threadpool(
                    _persist_publish_progress,
                    job_id,
                    sent_count,
                    failed_count,
                    failed_ids,
                )

            if delivered and idx < total_requested - 1:
                if delay_sec > 0:
                    await asyncio.sleep(delay_sec)
                elif is_group_or_channel:
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.01)

        final_status = "completed" if failed_count == 0 else ("partial_failed" if sent_count > 0 else "failed")
        await run_in_threadpool(
            _finalize_publish_job,
            job_id,
            final_status,
            sent_count,
            failed_count,
            failed_ids,
        )

        target_name = target_label or "المحادثة الخاصة"
        finish_msg = f"تمت عملية إرسال الاختبار إلى «{target_name}»."

        try:
            await bot_app.bot.send_message(
                chat_id=admin_id,
                text=finish_msg,
            )
        except Exception:
            pass

    await run_in_threadpool(
        write_audit_log,
        actor,
        "publish_quiz",
        finish_msg,
    )


@app.post("/api/publish_batch")
@app.post("/publish_batch")
def publish_batch(
    req: BatchPublishReq,
    background_tasks: BackgroundTasks,
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

            c.execute(
                """INSERT INTO publish_jobs
                   (actor_id, actor_name, actor_tag, bank_id, target_type, comm_id, target_label,
                    quiz_title, total_count, sent_count, failed_count, failed_question_ids,
                    delay_sec, shuffle_options, open_period, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, '[]'::jsonb, %s, %s, %s, 'queued')
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
                    len(ordered_valid_ids),
                    req.delay_sec,
                    req.shuffle_options,
                    req.open_period,
                ),
            )
            job_id = c.fetchone()[0]
        conn.commit()

    background_tasks.add_task(
        execute_publish_job,
        job_id,
        ordered_valid_ids,
        target_chat,
        target_label,
        topic_id,
        req.delay_sec,
        req.shuffle_options,
        req.open_period,
        req.quiz_title,
        actor,
        False,
    )
    return {
        "status": "queued",
        "job_id": job_id,
        "queued_count": len(ordered_valid_ids),
        "target_label": target_label,
    }


@app.get("/api/publish_jobs")
@app.get("/publish_jobs")
def get_publish_jobs(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    cleanup_old_logs()
    with PooledConn(readonly=True) as conn:
        with conn.cursor() as c:
            if actor["role"] == "owner" or actor["can_manage_admins"]:
                c.execute("""
                    SELECT id, actor_name, actor_tag, target_label, quiz_title,
                           total_count, sent_count, failed_count, failed_question_ids,
                           status, created_at, completed_at
                    FROM publish_jobs ORDER BY id DESC LIMIT 15
                """)
            else:
                c.execute("""
                    SELECT id, actor_name, actor_tag, target_label, quiz_title,
                           total_count, sent_count, failed_count, failed_question_ids,
                           status, created_at, completed_at
                    FROM publish_jobs WHERE actor_id = %s ORDER BY id DESC LIMIT 15
                """, (actor["user_id"],))
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
            "success_rate": rate,
            "status": r[9],
            "created_at": r[10].isoformat() if r[10] else None,
            "completed_at": r[11].isoformat() if r[11] else None,
        })
    return jobs


@app.post("/api/publish_jobs/retry_failed")
@app.post("/publish_jobs/retry_failed")
def retry_failed_questions(
    req: RetryFailedReq,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str = Header(default=""),
):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_publish", "لا تملك صلاحية إرسال الاختبارات.")

    with PooledConn() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT id, actor_id, bank_id, target_type, comm_id, target_label,
                          quiz_title, failed_question_ids, delay_sec, shuffle_options, open_period
                   FROM publish_jobs WHERE id = %s""",
                (req.job_id,),
            )
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="سجل عملية الإرسال غير موجود.")
            if actor["role"] != "owner" and int(row[1]) != int(actor["user_id"]):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعادة إرسال هذه العملية.")

            failed_ids = row[7] if isinstance(row[7], list) else (json.loads(row[7]) if row[7] else [])
            if not failed_ids:
                raise HTTPException(status_code=400, detail="لا توجد أسئلة متعذرة لإعادة إرسالها في هذه العملية.")

            target_type = row[3]
            comm_id = row[4]
            target_label = row[5]
            topic_id = None
            if target_type == "saved_comm" and comm_id:
                c.execute("SELECT title, chat_id, topic_id FROM communities WHERE id = %s", (comm_id,))
                comm_row = c.fetchone()
                if not comm_row:
                    raise HTTPException(status_code=404, detail="الوجهة المحفوظة غير موجودة.")
                target_label = comm_row[0]
                target_chat = comm_row[1].strip()
                topic_id = comm_row[2]
            else:
                target_chat = actor["user_id"]
                target_label = "المحادثة الخاصة"

            c.execute("SELECT id FROM questions WHERE id = ANY(%s)", (failed_ids,))
            existing_set = {x[0] for x in c.fetchall()}
            valid_failed_ids = [qid for qid in failed_ids if qid in existing_set]
            if not valid_failed_ids:
                raise HTTPException(status_code=400, detail="الأسئلة المتعذرة لم تعد موجودة في قاعدة البيانات.")

            c.execute(
                """INSERT INTO publish_jobs
                   (actor_id, actor_name, actor_tag, bank_id, target_type, comm_id, target_label,
                    quiz_title, total_count, sent_count, failed_count, failed_question_ids,
                    delay_sec, shuffle_options, open_period, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, '[]'::jsonb, %s, %s, %s, 'queued')
                   RETURNING id""",
                (
                    actor["user_id"],
                    actor["name"],
                    actor["tag"],
                    row[2],
                    target_type,
                    comm_id,
                    target_label,
                    row[6] or "",
                    len(valid_failed_ids),
                    row[8] or 0.0,
                    bool(row[9]),
                    row[10],
                ),
            )
            new_job_id = c.fetchone()[0]
        conn.commit()

    background_tasks.add_task(
        execute_publish_job,
        new_job_id,
        valid_failed_ids,
        target_chat,
        target_label,
        topic_id,
        row[8] or 0.0,
        bool(row[9]),
        row[10],
        row[6] or "",
        actor,
        True,
    )
    return {
        "status": "queued",
        "job_id": new_job_id,
        "retried_count": len(valid_failed_ids),
        "target_label": target_label,
    }


@app.get("/api/admins")
@app.get("/admins")
def list_admins(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    require_perm(actor, "can_manage_admins", "إدارة المشرفين غير متاحة لصلاحياتك الحالية.")
    owner = get_owner_id()
    with PooledConn(readonly=True) as conn:
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
def upsert_admin(req: AdminUpsertReq, x_telegram_init_data: str = Header(default="")):
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
@app.delete("/admins/{admin_id}")
def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
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
def get_logs(x_telegram_init_data: str = Header(default="")):
    actor = verify_actor(x_telegram_init_data)
    cleanup_old_logs()
    with PooledConn(readonly=True) as conn:
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
def clear_logs(x_telegram_init_data: str = Header(default="")):
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
