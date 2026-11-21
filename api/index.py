import os
import io
import json
import hmac
import hashlib
import urllib.parse
import asyncio
import random
from typing import Optional
import pandas as pd
import psycopg2
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9268644584:AAE8woNBoXzueYqpsT4x69efYcDgP1mPP9Y")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

app = FastAPI(title="Telegram Quiz Mini App v3")

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="متغير DATABASE_URL غير مضاف في إعدادات Vercel")
    clean_url = DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")
    return psycopg2.connect(clean_url, sslmode="require")

def init_db():
    conn = get_conn()
    c = conn.cursor()
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
    conn.commit()
    c.close()
    conn.close()

def get_owner_id() -> Optional[int]:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'owner_id'")
    row = c.fetchone()
    c.close()
    conn.close()
    return int(row[0]) if row else None

def set_owner_id(user_id: int, name: str = "Owner"):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO config (key, value) VALUES ('owner_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(user_id),))
    c.execute("INSERT INTO admins (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, name))
    conn.commit()
    c.close()
    conn.close()

def is_admin(user_id: int) -> bool:
    owner = get_owner_id()
    if owner is None:
        return False
    if user_id == owner:
        return True
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
    res = c.fetchone()
    c.close()
    conn.close()
    return res is not None

def verify_telegram_init_data(init_data: str) -> dict:
    init_db()
    if not init_data:
        owner = get_owner_id()
        if owner:
            return {"id": owner, "first_name": "Owner"}
        raise HTTPException(status_code=401, detail="افتح التطبيق من داخل تيليجرام أولاً لتسجيل المالك")

    parsed = dict(urllib.parse.parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        raise HTTPException(status_code=401, detail="Invalid InitData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if calculated_hash != hash_val:
        raise HTTPException(status_code=403, detail="فشل التحقق من هوية تيليجرام")

    user_info = json.loads(parsed.get("user", "{}"))
    user_id = user_info.get("id")
    if get_owner_id() is None and user_id:
        set_owner_id(user_id, user_info.get("first_name", "Owner"))
    if not is_admin(user_id):
        raise HTTPException(status_code=403, detail="⛔ غير مصرح لك بالدخول لهذا التطبيق")
    return user_info

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

class PublishReq(BaseModel):
    bank_id: int
    target_type: str
    chat_id: Optional[str] = None
    topic_id: Optional[int] = None
    delay_sec: float = 0.0
    shuffle_questions: bool = False
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
            await update.message.reply_text("⛔ عذراً، هذا البوت مخصص للمشرفين فقط.")
            return

        url = webapp_url or f"https://{os.environ.get('VERCEL_URL', '')}"
        keyboard = [[InlineKeyboardButton("🚀 فتح تطبيق إدارة الاختبارات (v3)", web_app=WebAppInfo(url=url))]]
        await update.message.reply_text(
            f"👋 أهلاً بك يا **{user.first_name}**!\n\n"
            "• اضغط على الزر بالأسفل لفتح **التطبيق الكامل** لإدارة الأسئلة، المجتمعات، والتوقيت.\n"
            "• أرسل `/id` داخل أي جروب أو قناة فرعية (Topic) أو حوّل رسالة من القناة لهنا لاستخراج الـ **Chat ID** تلقائياً.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def get_chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        msg = update.effective_message
        topic_id = getattr(msg, "message_thread_id", None)
        text = f"📌 **بيانات المحادثة / القناة:**\n• **الاسم:** `{chat.title or chat.first_name}`\n• **Chat ID:** `{chat.id}`"
        if topic_id:
            text += f"\n• **Topic ID:** `{topic_id}`"
        await msg.reply_text(text, parse_mode="Markdown")

    async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.forward_origin:
            origin = msg.forward_origin
            if hasattr(origin, "chat") and origin.chat:
                await msg.reply_text(
                    f"✅ **تم استخراج بيانات القناة/الجروب:**\n"
                    f"• **الاسم:** `{origin.chat.title}`\n"
                    f"• **Chat ID:** `{origin.chat.id}`",
                    parse_mode="Markdown"
                )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", get_chat_id_cmd))
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    return application

@app.get("/api/setup_webhook")
@app.get("/setup_webhook")
async def setup_webhook(request: Request):
    init_db()
    host = request.headers.get("host")
    base_url = f"https://{host}"
    webhook_url = f"{base_url}/api/webhook"

    bot_app = build_bot_app(base_url)
    async with bot_app:
        await bot_app.bot.set_webhook(url=webhook_url)
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📱 فتح التطبيق", web_app=WebAppInfo(url=base_url))
        )
    return {
        "status": "Webhook & Menu Button configured successfully! (v3)",
        "app_url": base_url,
        "webhook_url": webhook_url
    }

@app.post("/api/webhook")
@app.post("/webhook")
async def telegram_webhook(request: Request):
    host = request.headers.get("host")
    base_url = f"https://{host}"
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
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, name, (SELECT COUNT(*) FROM questions WHERE bank_id = banks.id) FROM banks ORDER BY id DESC")
    rows = c.fetchall()
    c.close()
    conn.close()
    return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]

@app.post("/api/banks")
@app.post("/banks")
async def create_bank(req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO banks (name) VALUES (%s) RETURNING id", (req.name.strip(),))
        bank_id = c.fetchone()[0]
        conn.commit()
        return {"id": bank_id, "name": req.name.strip()}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="يوجد مجموعة بنفس الاسم بالفعل")
    finally:
        c.close()
        conn.close()

@app.put("/api/banks/{bank_id}")
@app.put("/banks/{bank_id}")
async def rename_bank(bank_id: int, req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE banks SET name = %s WHERE id = %s", (req.name.strip(), bank_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/banks/{bank_id}")
@app.delete("/banks/{bank_id}")
async def delete_bank(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM banks WHERE id = %s", (bank_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/api/upload_csv")
@app.post("/upload_csv")
async def upload_csv(
    bank_name: str = Form(...),
    file: UploadFile = File(...),
    x_telegram_init_data: str = Header(default="")
):
    verify_telegram_init_data(x_telegram_init_data)
    content = await file.read()
    df = None
    for enc in ['utf-8-sig', 'utf-8', 'cp1256', 'windows-1256', 'latin1']:
        try:
            df = pd.read_csv(io.BytesIO(content), header=None, encoding=enc)
            break
        except Exception:
            continue
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail="تعذر قراءة ملف CSV أو الملف فارغ")

    first_ans = str(df.iloc[0, 5]).strip()
    if first_ans not in ['1', '2', '3', '4', '0', '1.0', '2.0', '3.0', '4.0']:
        df = df.iloc[1:]

    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO banks (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (bank_name.strip(),))
    c.execute("SELECT id FROM banks WHERE name = %s", (bank_name.strip(),))
    bank_id = c.fetchone()[0]

    added = 0
    for _, row in df.iterrows():
        if pd.isna(row[0]):
            continue
        q_text = str(row[0]).strip()
        opts = [str(row[i]).strip() if not pd.isna(row[i]) else "-" for i in range(1, 5)]
        ans_raw = int(float(str(row[5]).strip()))
        c_idx = ans_raw - 1 if 1 <= ans_raw <= 4 else 0
        expl = str(row[6]).strip() if len(row) > 6 and not pd.isna(row[6]) else ""
        c.execute("""INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                  (bank_id, q_text, opts[0], opts[1], opts[2], opts[3], c_idx, expl))
        added += 1

    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok", "added": added, "bank_id": bank_id}

@app.get("/api/banks/{bank_id}/questions")
@app.get("/banks/{bank_id}/questions")
async def get_questions(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC", (bank_id,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return [
        {"id": r[0], "question": r[1], "opt1": r[2], "opt2": r[3], "opt3": r[4], "opt4": r[5], "correct_idx": r[6], "explanation": r[7]}
        for r in rows
    ]

@app.post("/api/questions")
@app.post("/questions")
async def add_question(req: QuestionCreate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
              (req.bank_id, req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.put("/api/questions/{q_id}")
@app.put("/questions/{q_id}")
async def update_question(q_id: int, req: QuestionUpdate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""UPDATE questions SET question=%s, opt1=%s, opt2=%s, opt3=%s, opt4=%s, correct_idx=%s, explanation=%s WHERE id=%s""",
              (req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation, q_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/api/questions/{q_id}/move")
@app.post("/questions/{q_id}/move")
async def move_question(q_id: int, req: MoveQuestionReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE questions SET bank_id = %s WHERE id = %s", (req.target_bank_id, q_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/questions/{q_id}")
@app.delete("/questions/{q_id}")
async def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE id = %s", (q_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.get("/api/communities")
@app.get("/communities")
async def get_communities(x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, title, chat_id, topic_id FROM communities ORDER BY id DESC")
    rows = c.fetchall()
    c.close()
    conn.close()
    return [{"id": r[0], "title": r[1], "chat_id": r[2], "topic_id": r[3]} for r in rows]

@app.post("/api/communities")
@app.post("/communities")
async def add_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO communities (title, chat_id, topic_id) VALUES (%s, %s, %s)",
              (req.title.strip(), req.chat_id.strip(), req.topic_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/communities/{comm_id}")
@app.delete("/communities/{comm_id}")
async def delete_community(comm_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM communities WHERE id = %s", (comm_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/api/test_community")
@app.post("/test_community")
async def test_community(req: CommunityCreateReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    bot_app = build_bot_app()
    async with bot_app:
        try:
            kwargs = {"chat_id": req.chat_id.strip(), "text": "✅ تم التحقق من الاتصال! البوت جاهز لإرسال الاختبارات هنا."}
            if req.topic_id:
                kwargs["message_thread_id"] = req.topic_id
            await bot_app.bot.send_message(**kwargs)
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"فشل الاتصال: تأكد أن البوت مشرف (Admin) ولديه صلاحية إرسال الرسائل. ({e})")

@app.post("/api/publish")
@app.post("/publish")
async def publish_quiz(req: PublishReq, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    target_chat = user.get("id") if req.target_type == "private" else req.chat_id
    if not target_chat:
        raise HTTPException(status_code=400, detail="يرجى تحديد الوجهة أو اختيار مجتمع/قناة")

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC", (req.bank_id,))
    questions = c.fetchall()
    c.close()
    conn.close()

    if not questions:
        raise HTTPException(status_code=400, detail="المجموعة فارغة من الأسئلة")

    if req.shuffle_questions:
        random.shuffle(questions)

    bot_app = build_bot_app()
    async with bot_app:
        for idx, (q_text, o1, o2, o3, o4, c_idx, expl) in enumerate(questions):
            opts = [o1[:98], o2[:98], o3[:98], o4[:98]]
            if req.shuffle_options:
                indexed = list(enumerate(opts))
                random.shuffle(indexed)
                opts = [x[1] for x in indexed]
                c_idx = [i for i, x in enumerate(indexed) if x[0] == c_idx][0]

            kwargs = {
                "chat_id": target_chat,
                "question": q_text[:295],
                "options": opts,
                "type": Poll.QUIZ,
                "correct_option_id": c_idx,
                "is_anonymous": True
            }
            if expl:
                kwargs["explanation"] = expl[:195]
            if req.topic_id:
                kwargs["message_thread_id"] = req.topic_id
            if req.open_period and 5 <= req.open_period <= 600:
                kwargs["open_period"] = req.open_period

            await bot_app.bot.send_poll(**kwargs)
            wait_time = 0.35 if req.delay_sec <= 0 else req.delay_sec
            if idx < len(questions) - 1:
                await asyncio.sleep(wait_time)

    return {"status": "completed", "count": len(questions)}

@app.get("/api/admins")
@app.get("/admins")
async def list_admins(x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    owner = get_owner_id()
    if user.get("id") != owner:
        raise HTTPException(status_code=403, detail="للمالك فقط")
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, name FROM admins")
    rows = c.fetchall()
    c.close()
    conn.close()
    return [{"user_id": r[0], "name": r[1], "is_owner": r[0] == owner} for r in rows]

@app.post("/api/admins")
@app.post("/admins")
async def add_admin(req: AdminAddReq, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    if user.get("id") != get_owner_id():
        raise HTTPException(status_code=403, detail="للمالك فقط")
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO admins (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name", (req.user_id, req.name))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/admins/{admin_id}")
@app.delete("/admins/{admin_id}")
async def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    owner = get_owner_id()
    if user.get("id") != owner or admin_id == owner:
        raise HTTPException(status_code=403, detail="غير مسموح")
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE user_id = %s", (admin_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}
