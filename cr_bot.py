import os
import io
import sqlite3
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

genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-3.8-flash")

# ==============================================================================
# 2. LOCAL DATABASE INITIALIZATION
# ==============================================================================
def init_db():
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    
    # Store knowledge updates (routines, announcements, cancellations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Store student question logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS student_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT,
            question TEXT,
            bot_response TEXT,
            status TEXT,
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
    cursor.execute("SELECT source_type, content, timestamp FROM knowledge ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "No active schedule or routine updates logged yet."
    
    context = ""
    for r in rows:
        context += f"[{r[2]}] ({r[0]}): {r[1]}\n"
    return context

# ==============================================================================
# 3. SIDE A: CR ADMIN INTERFACE (PRIVATE DM ONLY)
# ==============================================================================
async def handle_cr_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != CR_USER_ID:
        return

    # A. Multimodal Routine Photo Upload
    if update.message.photo:
        await update.message.reply_text("📥 Processing uploaded image schedule/announcement...")
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        
        caption = update.message.caption or ""
        prompt = f"Extract all class schedules, room numbers, teacher info, and routine updates from this image. Extra note: {caption}"
        
        response = ai_model.generate_content([prompt, image])
        extracted_text = response.text
        
        save_knowledge("IMAGE_PARSE", extracted_text)
        await update.message.reply_text(f"✅ **Routine Image Processed & Stored!**\n\n**Extracted Information:**\n{extracted_text}")
        return

    # B. Text Commands / Updates
    cr_text = update.message.text
    if not cr_text:
        return

    # Check student logs query
    if any(k in cr_text.lower() for k in ["student", "log", "ask", "question", "issue"]):
        conn = sqlite3.connect("cr_assistant.db")
        cursor = conn.cursor()
        cursor.execute("SELECT student_name, question, bot_response, status FROM student_logs ORDER BY id DESC LIMIT 10")
        logs = cursor.fetchall()
        conn.close()
        
        if not logs:
            await update.message.reply_text("📋 No student queries logged yet.")
            return

        summary_prompt = f"Summarize these recent student queries and issues for the CR:\n{logs}"
        summary = ai_model.generate_content(summary_prompt).text
        await update.message.reply_text(f"📊 **Student Questions & Log Summary:**\n\n{summary}")
        return

    # Conversational update
    parse_prompt = f"Extract and organize class routine updates, cancellations, or teacher info from this message: '{cr_text}'."
    parsed_info = ai_model.generate_content(parse_prompt).text
    save_knowledge("CR_NOTE", parsed_info)
    
    await update.message.reply_text(f"🤖 **Class Memory Updated:**\n{parsed_info}")

# ==============================================================================
# 4. SIDE B: STUDENT INTERFACE (GROUP CHATS & STUDENT DMs)
# ==============================================================================
async def handle_student_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user_id = update.message.from_user.id
    # Skip private messages from the CR (handled by admin interface above)
    if user_id == CR_USER_ID and update.message.chat.type == 'private':
        return

    student_name = update.message.from_user.first_name or "Student"
    student_query = update.message.text
    
    current_knowledge = get_recent_knowledge()
    
    prompt = f"""
    You are the friendly, helpful Class Assistant Bot.
    Answer student queries based ONLY on the active knowledge base provided below.
    
    Active Knowledge Base:
    {current_knowledge}
    
    Student Name: {student_name}
    Student Question: {student_query}
    
    Instructions:
    1. Be polite, clear, and friendly.
    2. If the answer exists in the Knowledge Base (cancellations, routines, teacher info), give it directly.
    3. If the answer is UNKNOWN or missing, reply politely that you don't have that update yet and have flagged it for the CR.
    """
    
    bot_response = ai_model.generate_content(prompt).text
    
    status = "FLAGGED_FOR_CR" if "flagged it for the CR" in bot_response.lower() else "RESOLVED"
    
    conn = sqlite3.connect("cr_assistant.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO student_logs (student_name, question, bot_response, status) VALUES (?, ?, ?, ?)",
        (student_name, student_query, bot_response, status)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(bot_response)

# ==============================================================================
# 5. DUMMY HEALTH-CHECK SERVER (REQUIRED FOR RENDER FREE HARDWARE)
# ==============================================================================
def start_dummy_health_server():
    port = int(os.environ.get("PORT", 8080))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

# ==============================================================================
# 6. MAIN BOT RUNNER
# ==============================================================================
def main():
    # Start web server in background thread so cloud platforms stay active
    threading.Thread(target=start_dummy_health_server, daemon=True).start()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Route CR DMs
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.User(user_id=CR_USER_ID), handle_cr_messages))
    
    # Route Student queries
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_student_messages))

    print("🚀 CR Assistant Bot is running live...")
    app.run_polling()

if __name__ == "__main__":
    main()
