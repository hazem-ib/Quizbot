import os
import io
import json
import hmac
import hashlib
import urllib.parse
import asyncio
import random
from typing import Optional, List
import pandas as pd
import psycopg2
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll, MenuButtonWebApp
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9268644584:AAE8woNBoXzueYqpsT4x69efYcDgP1mPP9Y")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

app = FastAPI(title="Telegram Quiz Mini App v4")

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
    if not init_data or not init_data.strip():
        raise HTTPException(status_code=403, detail="⛔ غير مصرح: يجب فتح التطبيق من داخل تيليجرام حصراً.")

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
        raise HTTPException(status_code=403, detail="⛔ عذراً، هذا التطبيق مخصص للمالك والمشرفين فقط.")
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

class BulkPublishReq(BaseModel):
    bank_id: int
    question_ids: Optional[List[int]] = None
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
        keyboard = [[InlineKeyboardButton("🚀 فتح تطبيق إدارة الاختبارات", web_app=WebAppInfo(url=url))]]
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
        "status": "Webhook & Menu Button configured successfully! (v4)",
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

def parse_correct_index(ans_raw_val, opts: list) -> Optional[int]:
    if pd.isna(ans_raw_val):
        return None
    s = str(ans_raw_val).strip()
    if not s or s.lower() == 'nan':
        return None

    s_norm = " ".join(s.split()).lower()
    for i, opt in enumerate(opts):
        opt_norm = " ".join(str(opt).split()).lower()
        if s_norm == opt_norm and opt_norm != "-":
            return i

    num_map = {'1': 0, '2': 1, '3': 2, '4': 3, '1.0': 0, '2.0': 1, '3.0': 2, '4.0': 3}
    if s in num_map:
        return num_map[s]

    s_clean = s.upper().rstrip('.').rstrip(')').strip()
    letter_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3, 'أ': 0, 'ا': 0, 'ب': 1, 'ج': 2, 'د': 3}
    if s_clean in letter_map:
        return letter_map[s_clean]

    return None

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
            df = pd.read_csv(io.BytesIO(content), header=None, encoding=enc, sep=None, engine='python')
            break
        except Exception:
            continue
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail="تعذر قراءة ملف CSV أو الملف فارغ")

    if df.shape[1] < 6:
        raise HTTPException(status_code=400, detail=f"الملف يحتوي على {df.shape[1]} أعمدة فقط! يجب أن يحتوي على 6 أعمدة على الأقل.")

    first_opts = [str(df.iloc[0, i]).strip() if not pd.isna(df.iloc[0, i]) else "" for i in range(1, 5)]
    if parse_correct_index(df.iloc[0, 5], first_opts) is None:
        df = df.iloc[1:]

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO banks (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (bank_name.strip(),))
        c.execute("SELECT id FROM banks WHERE name = %s", (bank_name.strip(),))
        bank_id = c.fetchone()[0]

        added = 0
        for idx, row in df.iterrows():
            if pd.isna(row[0]) or str(row[0]).strip() == "":
                continue
            q_text = str(row[0]).strip()
            opts = [str(row[i]).strip() if not pd.isna(row[i]) and str(row[i]).strip() != "" else "-" for i in range(1, 5)]
            if all(o == "-" for o in opts):
                continue

            c_idx = parse_correct_index(row[5], opts)
            if c_idx is None:
                val_found = "" if pd.isna(row[5]) else str(row[5]).strip()
                raise HTTPException(
                    status_code=400,
                    detail=f"خطأ في الصف {idx+1} (السؤال: {q_text[:30]}...): قيمة العمود السادس غير صالحة ('{val_found}')."
                )

            expl = str(row[6]).strip() if df.shape[1] > 6 and not pd.isna(row[6]) else ""
            c.execute("""INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                      (bank_id, q_text, opts[0], opts[1], opts[2], opts[3], c_idx, expl))
            added += 1

        conn.commit()
        return {"status": "ok", "added": added, "bank_id": bank_id}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"خطأ أثناء الحفظ: {str(e)}")
    finally:
        c.close()
        conn.close()

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

# دالة العمل في الخلفية على الخادم لإرسال الكل معاً حتى لو أغلقت البوت فوراً
async def run_bulk_publish_task(questions: list, target_chat, req: BulkPublishReq, admin_user_id: int):
    bot_app = build_bot_app()
    sent_count = 0
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

            while True:
                try:
                    await bot_app.bot.send_poll(**kwargs)
                    sent_count += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(int(e.retry_after) + 1)
                except Exception:
                    break

            if idx < len(questions) - 1:
                if req.delay_sec > 0:
                    await asyncio.sleep(req.delay_sec)
                else:
                    # أسرع ما يمكن مع تجنب حظر الترتيب
                    await asyncio.sleep(0.15)

        try:
            await bot_app.bot.send_message(
                chat_id=admin_user_id,
                text=f"✅ اكتمل إرسال الاختبار في الخلفية! تم إرسال **{sent_count}** من **{len(questions)}** سؤال.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

@app.post("/api/publish_all")
@app.post("/publish_all")
async def publish_all_background(
    req: BulkPublishReq,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str = Header(default="")
):
    user = verify_telegram_init_data(x_telegram_init_data)
    admin_user_id = user.get("id")
    target_chat = admin_user_id if req.target_type == "private" else req.chat_id
    if not target_chat:
        raise HTTPException(status_code=400, detail="يرجى تحديد الوجهة أو اختيار مجتمع/قناة")

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = %s ORDER BY id ASC", (req.bank_id,))
    rows = c.fetchall()
    c.close()
    conn.close()

    if req.question_ids is not None:
        allowed = set(req.question_ids)
        rows = [r for r in rows if r[0] in allowed]

    questions = [(r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]
    if not questions:
        raise HTTPException(status_code=400, detail="لا توجد أسئلة محددة للإرسال")

    if req.shuffle_questions:
        random.shuffle(questions)

    background_tasks.add_task(run_bulk_publish_task, questions, target_chat, req, admin_user_id)
    return {"status": "started", "count": len(questions)}

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

@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def catch_all(full_path: str, request: Request):
    if "setup_webhook" in full_path:
        return await setup_webhook(request)
    if "webhook" in full_path and request.method == "POST":
        return await telegram_webhook(request)
    raise HTTPException(status_code=404, detail=f"Path received by FastAPI: /{full_path}")
