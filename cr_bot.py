import os
import io
import sqlite3
import datetime
import pytz
import threading
import http.server
import socketserver
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# ==============================================================================
# 1. ENVIRONMENT VARIABLES & SETUP
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CR_USER_ID = int(os.getenv("CR_TELEGRAM_USER_ID", "0"))

# Specify your local timezone for the 7:00 AM daily notification
# (e.g., 'Asia/Kolkata', 'Asia/Dhaka', 'UTC', 'America/New_York')
TIMEZONE_STR = os.getenv("BOT_TIMEZONE", "Asia/Kolkata")
LOCAL_TZ = pytz.timezone(TIMEZONE_STR)

genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-3.8-flash")

# ==============================================================================
# 2. LOCAL DATABASE & MEMORY
# ==============================================================================
def init_db():
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    
    # Store knowledge updates for Section GH
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Store group chat ID for 7:00 AM broadcasts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # Store student question logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS student_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT,
            question TEXT,
            bot_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def save_knowledge(source_type: str, content: str):
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO knowledge (source_type, content) VALUES (?, ?)", (source_type, content))
    conn.commit()
    conn.close()

def get_recent_knowledge():
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT source_type, content, timestamp FROM knowledge ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "No active schedule or routine updates logged yet."
    
    context = ""
    for r in rows:
        context += f"[{r[2]}] ({r[0]}): {r[1]}\n"
    return context

def set_group_chat_id(chat_id: int):
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('group_chat_id', ?)", (str(chat_id),))
    conn.commit()
    conn.close()

def get_group_chat_id():
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'group_chat_id'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None

# ==============================================================================
# 3. CR CONVERSATIONAL ADMIN INTERFACE (PRIVATE DM)
# ==============================================================================
async def handle_cr_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != CR_USER_ID:
        return

    # A. Multi-Section Routine Image Upload (Isolates Section GH)
    if update.message.photo:
        await update.message.reply_text("🔍 Analyzing routine image... Filtering specifically for **Section GH**...")
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        
        caption = update.message.caption or ""
        prompt = f"""
        Look at this routine/timetable image carefully. It contains schedules for multiple sections.
        Your task:
        1. Extract the weekly routine ONLY for **Section GH**. Ignore all other sections.
        2. List each day (Monday - Sunday), class times, subject names, room numbers, and teacher names for Section GH.
        3. Include any extra notes from the CR: '{caption}'.
        """
        
        response = ai_model.generate_content([prompt, image])
        extracted_text = response.text
        
        save_knowledge("MASTER_ROUTINE_GH", extracted_text)
        await update.message.reply_text(f"✅ **Section GH Routine Extracted & Stored!**\n\n{extracted_text}")
        return

    # B. Natural Conversational Reasoning (e.g. "Prof. Smith said...")
    cr_text = update.message.text
    if not cr_text:
        return

    # View student logs
    if any(k in cr_text.lower() for k in ["student", "log", "ask", "question", "issue"]):
        conn = sqlite3.connect("cr_assistant.db")
        cursor = conn.cursor()
        cursor.execute("SELECT student_name, question, bot_response FROM student_logs ORDER BY id DESC LIMIT 10")
        logs = cursor.fetchall()
        conn.close()
        
        if not logs:
            await update.message.reply_text("📋 No student queries logged yet.")
            return

        summary = ai_model.generate_content(f"Summarize these recent student queries for the CR:\n{logs}").text
        await update.message.reply_text(f"📊 **Student Query Summary:**\n\n{summary}")
        return

    # Parse CR natural conversation & reasoning
    parse_prompt = f"""
    The Class Representative (CR) for Section GH sent this explanation/update:
    '{cr_text}'
    
    Understand the reasoning, conversation context with professors, room shifts, or cancellations.
    Summarize and format this clearly as an official update for Section GH's active memory.
    """
    parsed_info = ai_model.generate_content(parse_prompt).text
    save_knowledge("CR_CONVERSATION_UPDATE", parsed_info)
    
    await update.message.reply_text(f"🤖 **Understood & Memory Updated for Section GH:**\n\n{parsed_info}")

# ==============================================================================
# 4. STUDENT GROUP CHAT & 7:00 AM BROADCASTER
# ==============================================================================
async def handle_student_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Auto-detect and register Section GH Group Chat ID
    if update.message.chat.type in ['group', 'supergroup']:
        set_group_chat_id(update.message.chat.id)

    user_id = update.message.from_user.id
    if user_id == CR_USER_ID and update.message.chat.type == 'private':
        return

    student_name = update.message.from_user.first_name or "Student"
    student_query = update.message.text
    current_knowledge = get_recent_knowledge()
    
    prompt = f"""
    You are the interactive, friendly Class Assistant Bot for **Section GH**.
    
    Active Section GH Knowledge Base:
    {current_knowledge}
    
    Student: {student_name}
    Message: {student_query}
    
    Instructions:
    1. Answer questions about Section GH routines, cancellations, and room updates accurately using the Knowledge Base.
    2. If the user is asking general questions, homework help, or chit-chatting, respond warmly like a friendly AI assistant.
    3. Keep answers interactive, clear, and easy to read.
    """
    
    bot_response = ai_model.generate_content(prompt).text

    # Log query
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO student_logs (student_name, question, bot_response) VALUES (?, ?, ?)",
                   (student_name, student_query, bot_response))
    conn.commit()
    conn.close()

    await update.message.reply_text(bot_response)

# 7:00 AM Daily Schedule Broadcast Function
async def send_daily_7am_routine(context: ContextTypes.DEFAULT_TYPE):
    group_chat_id = get_group_chat_id()
    if not group_chat_id:
        return

    today_name = datetime.datetime.now(LOCAL_TZ).strftime("%A")
    current_knowledge = get_recent_knowledge()

    prompt = f"""
    Based on Section GH's active knowledge base below, generate an attractive, interactive morning summary for TODAY ({today_name}).
    
    Active Knowledge Base:
    {current_knowledge}
    
    Formatting rules:
    - Include subject names, class times, room numbers, and assigned professors.
    - Highlight any class cancellations or rescheduled times.
    - Keep the tone motivating, friendly, and structured with clean emojis.
    """
    
    daily_routine = ai_model.generate_content(prompt).text
    message = f"🌅 **Good Morning Section GH!**\n\n📅 **Today's Routine ({today_name}):**\n\n{daily_routine}"
    
    await context.bot.send_message(chat_id=group_chat_id, text=message, parse_mode="Markdown")

# ==============================================================================
# 5. DUMMY HEALTH-CHECK SERVER
# ==============================================================================
def start_dummy_health_server():
    port = int(os.environ.get("PORT", 8080))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

# ==============================================================================
# 6. MAIN BOT RUNNER & SCHEDULER
# ==============================================================================
def main():
    threading.Thread(target=start_dummy_health_server, daemon=True).start()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Schedule 7:00 AM Daily Notification
    target_time = datetime.time(hour=7, minute=0, second=0, tzinfo=LOCAL_TZ)
    app.job_queue.run_daily(send_daily_7am_routine, time=target_time)

    # Handlers
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.User(user_id=CR_USER_ID), handle_cr_messages))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_student_messages))

    print(f"🚀 CR Assistant Bot for Section GH is live! Daily 7 AM notification active ({TIMEZONE_STR}).")
    app.run_polling()

if __name__ == "__main__":
    main()
   
