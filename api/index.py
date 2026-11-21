import sqlite3
import pandas as pd
import io
import os
import json
import hmac
import hashlib
import urllib.parse
import asyncio
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================= الإعدادات الأساسية =================
BOT_TOKEN = "9268644584:AAE8woNBoXzueYqpsT4x69efYcDgP1mPP9Y"
# ضع رابط HTTPS الخاص بالسيرفر هنا (مثلاً من ngrok أو Render أو VPS) ليظهر زر فتح التطبيق داخل تيليجرام
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-https-domain.com")
DB_NAME = "quiz_miniapp.db"

app = FastAPI(title="Telegram Quiz Mini App")
bot_application: Optional[Application] = None

# ================= قاعدة البيانات =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, name TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS banks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
    c.execute("""CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bank_id INTEGER,
                    question TEXT,
                    opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT,
                    correct_idx INTEGER,
                    explanation TEXT DEFAULT '',
                    FOREIGN KEY(bank_id) REFERENCES banks(id) ON DELETE CASCADE)""")
    conn.commit()
    conn.close()

def get_owner_id() -> Optional[int]:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'owner_id'")
    row = c.fetchone()
    conn.close()
    return int(row[0]) if row else None

def set_owner_id(user_id: int, name: str = "Owner"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('owner_id', ?)", (str(user_id),))
    c.execute("INSERT OR IGNORE INTO admins (user_id, name) VALUES (?, ?)", (user_id, name))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    owner = get_owner_id()
    if owner is None:
        return False
    if user_id == owner:
        return True
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

# التحقق الأمني من هوية مستخدم Telegram Mini App
def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        # للتجربة المحلية في المتصفح إذا لم يفتح من تيليجرام
        owner = get_owner_id()
        if owner:
            return {"id": owner, "first_name": "Owner"}
        raise HTTPException(status_code=401, detail="Missing Telegram InitData")
    
    parsed = dict(urllib.parse.parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        raise HTTPException(status_code=401, detail="Invalid InitData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if calculated_hash != hash_val:
        raise HTTPException(status_code=403, detail="Authentication failed")

    user_info = json.loads(parsed.get("user", "{}"))
    user_id = user_info.get("id")
    if get_owner_id() is None and user_id:
        set_owner_id(user_id, user_info.get("first_name", "Owner"))
    if not is_admin(user_id):
        raise HTTPException(status_code=403, detail="غير مصرح لك بالدخول لهذا التطبيق")
    return user_info

# ================= نماذج البيانات API =================
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

class PublishReq(BaseModel):
    bank_id: int
    target_type: str  # "private" or "group"
    chat_id: Optional[str] = None
    topic_id: Optional[int] = None
    delay_sec: float = 1.5
    shuffle_questions: bool = False
    shuffle_options: bool = False
    open_period: Optional[int] = None  # مؤقت زمني بالثواني للسؤال (5 - 600)

class AdminAddReq(BaseModel):
    user_id: int
    name: Optional[str] = "Admin"

# ================= نقاط اتصال التطبيق (REST API) =================
@app.get("/")
async def serve_app():
    return FileResponse("static/index.html")

@app.get("/api/me")
async def get_me(x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    uid = user.get("id")
    return {
        "user": user,
        "is_owner": uid == get_owner_id(),
        "owner_id": get_owner_id()
    }

@app.get("/api/banks")
async def get_banks(x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, name, (SELECT COUNT(*) FROM questions WHERE bank_id = banks.id) FROM banks ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]

@app.post("/api/banks")
async def create_bank(req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO banks (name) VALUES (?)", (req.name.strip(),))
        conn.commit()
        bank_id = c.lastrowid
        return {"id": bank_id, "name": req.name.strip()}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="يوجد مجموعة بنفس الاسم بالفعل")
    finally:
        conn.close()

@app.put("/api/banks/{bank_id}")
async def rename_bank(bank_id: int, req: BankRenameReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE banks SET name = ? WHERE id = ?", (req.name.strip(), bank_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/banks/{bank_id}")
async def delete_bank(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE bank_id = ?", (bank_id,))
    c.execute("DELETE FROM banks WHERE id = ?", (bank_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/upload_csv")
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

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banks (name) VALUES (?)", (bank_name.strip(),))
    c.execute("SELECT id FROM banks WHERE name = ?", (bank_name.strip(),))
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
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (bank_id, q_text, opts[0], opts[1], opts[2], opts[3], c_idx, expl))
        added += 1

    conn.commit()
    conn.close()
    return {"status": "ok", "added": added, "bank_id": bank_id}

@app.get("/api/banks/{bank_id}/questions")
async def get_questions(bank_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = ?", (bank_id,))
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "question": r[1], "opt1": r[2], "opt2": r[3], "opt3": r[4], "opt4": r[5], "correct_idx": r[6], "explanation": r[7]}
        for r in rows
    ]

@app.post("/api/questions")
async def add_question(req: QuestionCreate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""INSERT INTO questions (bank_id, question, opt1, opt2, opt3, opt4, correct_idx, explanation)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (req.bank_id, req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.put("/api/questions/{q_id}")
async def update_question(q_id: int, req: QuestionUpdate, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""UPDATE questions SET question=?, opt1=?, opt2=?, opt3=?, opt4=?, correct_idx=?, explanation=? WHERE id=?""",
              (req.question, req.opt1, req.opt2, req.opt3, req.opt4, req.correct_idx, req.explanation, q_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/questions/{q_id}/move")
async def move_question(q_id: int, req: MoveQuestionReq, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE questions SET bank_id = ? WHERE id = ?", (req.target_bank_id, q_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/questions/{q_id}")
async def delete_question(q_id: int, x_telegram_init_data: str = Header(default="")):
    verify_telegram_init_data(x_telegram_init_data)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE id = ?", (q_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/publish")
async def publish_quiz(req: PublishReq, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    target_chat = user.get("id") if req.target_type == "private" else req.chat_id
    if not target_chat:
        raise HTTPException(status_code=400, detail="يرجى تحديد معرّف الجروب أو القناة")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT question, opt1, opt2, opt3, opt4, correct_idx, explanation FROM questions WHERE bank_id = ?", (req.bank_id,))
    questions = c.fetchall()
    conn.close()

    if not questions:
        raise HTTPException(status_code=400, detail="المجموعة فارغة من الأسئلة")

    import random
    if req.shuffle_questions:
        random.shuffle(questions)

    async def send_task():
        for q_text, o1, o2, o3, o4, c_idx, expl in questions:
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

            await bot_application.bot.send_poll(**kwargs)
            await asyncio.sleep(max(0.8, req.delay_sec))

    asyncio.create_task(send_task())
    return {"status": "started", "count": len(questions)}

# ================= إدارة الأدمنز (للمالك فقط) =================
@app.get("/api/admins")
async def list_admins(x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    if user.get("id") != get_owner_id():
        raise HTTPException(status_code=403, detail="للمالك فقط")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id, name FROM admins")
    rows = c.fetchall()
    conn.close()
    owner = get_owner_id()
    return [{"user_id": r[0], "name": r[1], "is_owner": r[0] == owner} for r in rows]

@app.post("/api/admins")
async def add_admin(req: AdminAddReq, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    if user.get("id") != get_owner_id():
        raise HTTPException(status_code=403, detail="للمالك فقط")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO admins (user_id, name) VALUES (?, ?)", (req.user_id, req.name))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/admins/{admin_id}")
async def remove_admin(admin_id: int, x_telegram_init_data: str = Header(default="")):
    user = verify_telegram_init_data(x_telegram_init_data)
    owner = get_owner_id()
    if user.get("id") != owner or admin_id == owner:
        raise HTTPException(status_code=403, detail="غير مسموح")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ================= أوامر بوت تيليجرام لفتح التطبيق =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if get_owner_id() is None:
        set_owner_id(user.id, user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("⛔ عذراً، هذا التطبيق مخصص للمشرفين فقط.")
        return

    keyboard = [[InlineKeyboardButton("🚀 فتح تطبيق إدارة الاختبارات (App)", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"👋 أهلاً بك يا **{user.first_name}**!\n\n"
        "اضغط على الزر بالأسفل لفتح **التطبيق الكامل (Mini App)** داخل تيليجرام لإدارة ملفات CSV، تعديل الأسئلة، ونشرها في الجروبات:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@app.on_event("startup")
async def on_startup():
    global bot_application
    init_db()
    bot_application = Application.builder().token(BOT_TOKEN).build()
    bot_application.add_handler(CommandHandler("start", start_cmd))
    await bot_application.initialize()
    await bot_application.start()
    await bot_application.updater.start_polling()
    # ضبط زر القائمة الرئيسي في تيليجرام ليفتح التطبيق مباشرة
    try:
        from telegram import MenuButtonWebApp
        await bot_application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📱 فتح التطبيق", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as e:
        print("Menu button setup info:", e)

@app.on_event("shutdown")
async def on_shutdown():
    if bot_application:
        await bot_application.updater.stop()
        await bot_application.stop()
        await bot_application.shutdown()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
