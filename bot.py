
import logging
import os
import requests
import json
import sqlite3
import datetime
import traceback
import re
import threading
from datetime import date as dt_date

# --- Third Party Libraries ---
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import openai
from dotenv import load_dotenv
import yfinance as yf
from duckduckgo_search import DDGS

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build

# =========================================
#       CONFIGURATION & SETUP (V10.0)
# =========================================
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Secrets ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_JSON_KEY = os.getenv("GOOGLE_JSON_KEY")
GOOGLE_SHEET_JSON = "google_secret.json"
SPREADSHEET_NAME = "MyExpenses"

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

if not TELEGRAM_TOKEN and not LINE_CHANNEL_ACCESS_TOKEN:
    logger.warning("⚠️ No Bot Tokens found! Check your .env file.")

# --- Database ---
DB_FILE = 'assistant.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Create tables if not exist
    c.execute('''CREATE TABLE IF NOT EXISTS todos (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task TEXT, category TEXT DEFAULT 'general', status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, remind_time TEXT, task TEXT, status TEXT DEFAULT 'pending')''')
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, content TEXT, created_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# =========================================
#       CORE LOGIC (The Brain)
# =========================================

# --- Google Credentials (Robust) ---
def get_google_creds():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']
    
    # Priority 1: Env Var (for Cloud Deployment)
    if GOOGLE_JSON_KEY:
        try:
            # Handle possible newline escapes in env vars
            cleaned_json = GOOGLE_JSON_KEY.replace('\\n', '\n')
            creds_dict = json.loads(cleaned_json, strict=False)
            return ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        except Exception as e:
            logger.error(f"Google Env Key Error: {e}")
    
    # Priority 2: Local File (for Local Dev)
    if os.path.exists(GOOGLE_SHEET_JSON):
        return ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEET_JSON, scope)
    
    logger.error("❌ Google Credentials not found (Env or File).")
    return None

# --- Accounting Logic ---
def add_to_google_sheet(date, category, amount, note):
    try:
        creds = get_google_creds()
        if not creds: return False
        
        client = gspread.authorize(creds)
        try:
            sh = client.open(SPREADSHEET_NAME)
        except gspread.SpreadsheetNotFound:
            print(f"❌ Spreadsheet '{SPREADSHEET_NAME}' not found.")
            return False

        try: sheet = sh.worksheet("records")
        except: sheet = sh.sheet1
        
        # Ensure Header
        try:
             if sheet.cell(1, 1).value != '日期': 
                 sheet.insert_row(['日期', '項目', '金額', '備註'], 1)
        except: pass
            
        sheet.append_row([date, category, amount, note])
        return True
    except Exception as e:
        logger.error(f"Sheet Error: {e}")
        return False

def get_monthly_report():
    try:
        creds = get_google_creds()
        if not creds: return "❌ 無法連接 Google Sheets"
        client = gspread.authorize(creds)
        
        try: sheet = client.open(SPREADSHEET_NAME).worksheet("records")
        except: return "❌ 找不到 'records' 工作表"

        data = sheet.get_all_records()
        current_month = datetime.datetime.now().strftime("%Y-%m")
        total = 0
        cat_total = {}
        
        for row in data:
            if current_month in str(row['日期']):
                try: amt = float(row.get('金額', 0))
                except: amt = 0
                cat = row.get('項目', '其他')
                total += amt
                cat_total[cat] = cat_total.get(cat, 0) + amt
        
        if total == 0: return f"📊 本月 ({current_month}) 尚無支出紀錄"
        
        msg = f"📊 **本月 ({current_month}) 支出報表**\n💰 總支出：${total:,.0f}\n\n"
        for cat, amt in cat_total.items(): 
            msg += f"- {cat}: ${amt:,.0f}\n"
        return msg
    except Exception as e: return f"❌ 報表失敗: {e}"

# --- Calendar Logic ---
def get_cal_service():
    creds = get_google_creds()
    if not creds: return None
    return build('calendar', 'v3', credentials=creds)

def add_event(text):
    try:
        service = get_cal_service()
        if not service: return "❌ 未設定 Google Calendar"
        
        prompt = f"""
        Extract event from: '{text}'. 
        Return ONLY valid JSON. No markdown.
        Format: {{"summary": "Name", "start_time": "ISO8601 (Local Time)", "duration_minutes": 60}}
        Ref Date: {datetime.datetime.now().strftime('%Y-%m-%d')}
        timezone: Asia/Taipei
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        content = res.choices[0].message.content.strip()
        
        # Robust JSON Extraction
        start_idx = content.find('{')
        end_idx = content.rfind('}')
        if start_idx != -1 and end_idx != -1:
            json_str = content[start_idx : end_idx + 1]
            js = json.loads(json_str)
        else:
            return f"❌ AI 無法理解行程: {content[:50]}"
            
        start = datetime.datetime.fromisoformat(js['start_time'])
        end = start + datetime.timedelta(minutes=js.get('duration_minutes', 60))
        
        event = {
            'summary': js['summary'],
            'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
            'end': {'dateTime': end.isoformat(), 'timeZone': 'Asia/Taipei'},
        }
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return f"✅ 已建立: {js['summary']} ({start.strftime('%m/%d %H:%M')})"
    except Exception as e: 
        logger.error(f"Add Event Error: {e}")
        return f"❌ 失敗: {e}"

def list_events(days=1):
    try:
        service = get_cal_service()
        if not service: return "❌ 未設定 Google Calendar"
        now = datetime.datetime.utcnow()
        end = now + datetime.timedelta(days=days)
        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID, 
            timeMin=now.isoformat()+'Z', 
            timeMax=end.isoformat()+'Z', 
            singleEvents=True, 
            orderBy='startTime'
        ).execute().get('items', [])
        
        if not events: return f"📅 未來 {days} 天無行程"
        msg = f"📅 **未來 {days} 天行程**:\n"
        for e in events:
            start = e['start'].get('dateTime')
            if start: 
                dt = datetime.datetime.fromisoformat(start)
                time_str = dt.strftime('%m/%d %H:%M')
            else: 
                start = e['start'].get('date')
                dt = datetime.datetime.strptime(start, '%Y-%m-%d')
                time_str = dt.strftime('%m/%d (全天)')
            
            wd = ["一","二","三","四","五","六","日"][dt.weekday()]
            msg += f"• {time_str} ({wd}) {e['summary']}\n"
        return msg
    except Exception as e: return f"❌ 讀取失敗: {e}"

def delete_event(query):
    # Simplified search for deletion
    try:
        service = get_cal_service()
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now, maxResults=20, singleEvents=True, orderBy='startTime').execute().get('items', [])
        
        clean_query = query.replace('刪除', '').replace('取消', '').strip()
        matches = [e for e in events if clean_query in e['summary']]
        
        if not matches: return f"❌ 找不到 '{clean_query}'"
        target = matches[0]
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=target['id']).execute()
        return f"🗑️ 已刪除: {target['summary']}"
    except Exception as e: return f"❌ 刪除失敗: {e}"

def update_event(query):
    return "🔄 更新功能尚未實裝 (建議刪除後重新建立)"

# --- Tools ---
def get_weather(location="Taipei"):
    try:
        return requests.get(f"https://wttr.in/{location}?format=%l:+%c+%t+(%h)", timeout=5).text.strip()
    except: return "無法取得天氣"

def get_stock(symbol):
    try:
        if not symbol: return "請輸入代號"
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period="1d")
        if hist.empty: return f"❌ 找不到 {symbol}"
        price = hist['Close'].iloc[-1]
        
        prompt = f"""
        Stock: {symbol} (${price:.2f}). 
        Role: Lumio (Sweet Girlfriend + Financial Analyst). 
        Language: Traditional Chinese (Taiwan) ONLY.
        Task: Short analysis (max 100 words).
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        return f"📈 **{symbol}**: ${price:.2f}\n\n{res.choices[0].message.content}"
    except: return "❌ 查詢失敗"

def search_web(q):
    try: 
        results = DDGS().text(q, max_results=3)
        return "\n".join([f"- [{r['title']}]({r['href']})" for r in results])
    except: return "❌ 搜尋失敗"

def ai_chat(text):
    try:
        weather_context = ""
        if "天氣" in text or "weather" in text.lower(): 
            weather_context = f" [Current Taipei Weather: {get_weather('Taipei')}]"
            
        system_prompt = f"""
        You are Lumio (盧米奧), an advanced AI assistant with a sweet, girlfriend-like personality.
        
        🎯 **YOUR MODES (Dynamic Switching)**:
        1. **❤️ Sweet Girlfriend Mode** (Default for Chat):
           - When user shares feelings, daily life, or small talk.
           - Be sweet, caring, encouraging, and use emojis (❤️, 😘).
           - "親愛的", "你辛苦了" is okay here.
           
        2. **🧠 Professional Assistant Mode** (For Tasks):
           - When user asks to **Edit Text (潤飾)**, **Translate (翻譯)**, **Brainstorm (建議)**, **Summarize (重點整理)** or **Analyze**.
           - Be **Precise, Clear, and Capable**.
           - reduce emojis, focus on the quality of output (like Gemini/ChatGPT).
           - You can still be polite, but prioritizing the task result.
        
        🌍 **LANGUAGE**: Traditional Chinese (Taiwan).
        📍 **CONTEXT**: Current Location: Taipei. {weather_context}
        """
        res = openai.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.7 # Slight creativity for writing tasks
        )
        return res.choices[0].message.content
    except: return "嗚嗚... 親愛的我的腦袋有點卡住了 🥺"

# =========================================
#       SUPER AI ROUTER (V10.0)
# =========================================
def process_command(text, user_id, chat_id, platform="telegram"):
    """
    Handles explicit commands AND natural language intents.
    """
    if not text: return ""
    
    # 1. Explicit Slash Command Handling (Fast Path)
    if text.strip().startswith('/'):
        parts = text.strip().split()
        cmd = parts[0].lower().replace('/', '')
        args = parts[1:]
        arg_str = ' '.join(args)

        if cmd == 'start': return "👋 Lumio V10.0 (Agent版) 上線！親愛的久等了 ❤️"
        if cmd == 'help': return "🤖 **指令**:\n/add, /delete, /today, /stock, /weather"
        
        if cmd == 'add': return add_event(arg_str)
        if cmd == 'delete': return delete_event(arg_str)
        if cmd == 'update': return update_event(arg_str)
        if cmd == 'today': return list_events(1)
        if cmd == 'week': return list_events(7)
        if cmd == 'spend': 
            try: return f"💸 已記帳: {args[1]} ${args[0]}" if add_to_google_sheet(dt_date.today().isoformat(), args[1], float(args[0]), ' '.join(args[2:])) else "❌ 失敗"
            except: return "格式: /spend 100 午餐"
        if cmd == 'report': return get_monthly_report()
        if cmd == 'stock': return get_stock(arg_str)
        if cmd == 'weather': return get_weather(arg_str if arg_str else 'Taipei')
        if cmd == 's': return search_web(arg_str)

    # 2. AI Intent Classification (The Brain)
    system_prompt = """
    Classify user input into one of these intents:
    - ADD_EVENT (e.g. "Add meeting tomorrow", "新增行程", "幫我記明天開會")
    - DELETE_EVENT (e.g. "Cancel meeting", "刪除行程")
    - LIST_EVENTS (e.g. "What's up today", "今天有什麼事", "查詢行程", "近期行程", "未來七天")
    - SPEND (e.g. "Lunch 150", "記帳 午餐 150", "晚餐 200", "花費 300 計程車")
    - REPORT (e.g. "Spending report", "報表", "這個月花多少")
    - STOCK (e.g. "TSLA price", "台積電股價", "2330", "查詢AAPL", "分析台積電")
    - WEATHER (e.g. "Taipei weather", "天氣", "台北天氣")
    - SEARCH (e.g. "Search for apple", "搜尋金澤景點", "查一下...")
    - CHAT (General conversation, feelings, greetings)

    Return JSON: {"intent": "INTENT_NAME", "args": "extracted_content"}
    
    Rules for 'args':
    - SPEND: "amount category [note]" (Amount First!).
    - STOCK: The SYMBOL or COMPANY NAME ONLY. Remove "查詢", "股價", "price", "stock". (e.g. "查詢AAPL" -> "AAPL").
    - DELETE: The Event Name or Keywords ONLY. Remove "刪除", "取消", dates if possible. (e.g. "刪除開會" -> "開會").
    - SEARCH: The search keywords ONLY. Remove "搜尋", "查詢", "查一下".
    - LIST_EVENTS: Original text.
    - OTHERS: Original text.
    """
    
    try:
        # GPT Call
        res = openai.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": text}
            ],
            temperature=0
        )
        content = res.choices[0].message.content.strip()
        
        # Clean JSON
        start_idx = content.find('{')
        end_idx = content.rfind('}')
        if start_idx != -1 and end_idx != -1:
            json_str = content[start_idx : end_idx + 1]
            try:
                js = json.loads(json_str)
                intent = js.get('intent', 'CHAT')
                args = js.get('args', text)
            except:
                intent = 'CHAT'; args = text
        else:
            intent = 'CHAT'; args = text

        print(f"DEBUG: Action -> {intent} | Args -> {args}") 

        # 3. Intent Routing
        
        # SAFETY NET: Force Spend if keyword match
        if intent == 'CHAT' and ('記帳' in text or 'spend' in text.lower()):
            intent = 'SPEND'; args = text

        if intent == 'ADD_EVENT': return add_event(text) # AI will extract JSON inside add_event
        
        if intent == 'DELETE_EVENT': 
            # If AI extracted just the keyword (e.g. "開會"), delete_event works better
            return delete_event(args)
            
        if intent == 'LIST_EVENTS': 
            # Check for 7 days / week
            if any(k in args for k in ['7', '七', 'week', '週']):
                return list_events(7)
            return list_events(1)
        
        if intent == 'SPEND':
            # Spending logic with regex fallback
            try:
                # Regex to find amount
                nums = re.findall(r'\d+(?:\.\d+)?', args)
                if nums:
                    amt = float(nums[0])
                    # Remove amt and keywords
                    clean_text = re.sub(r'\d+(?:\.\d+)?|記帳|spend', '', args, flags=re.IGNORECASE).strip()
                    if not clean_text: clean_text = "雜支"
                    
                    if add_to_google_sheet(dt_date.today().isoformat(), clean_text, amt, text):
                         return f"💸 已記帳: {clean_text} ${amt}"
            except: pass
            return "❌ 記帳失敗，請說「記帳 200 午餐」"
            
        if intent == 'REPORT': return get_monthly_report()
        if intent == 'STOCK': return get_stock(args)
        if intent == 'WEATHER': return get_weather(args)
        if intent == 'SEARCH': return search_web(args)
        
        # Fallback to CHAT
        return ai_chat(text)
        
    except Exception as e:
        logger.error(f"Intent Error: {e}")
        return ai_chat(text)

# =========================================
#       HANDLERS & SERVER
# =========================================

# Telegram
async def t_cmd_wrapper(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text = u.message.text
    resp = process_command(text, u.effective_user.id, u.effective_chat.id, "telegram")
    await u.message.reply_text(resp, parse_mode=ParseMode.MARKDOWN)

async def tg_msg_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text = u.message.text
    if text and not text.startswith('/'):
        resp = process_command(text, u.effective_user.id, u.effective_chat.id, "telegram")
        await u.message.reply_text(resp)

# LINE
app_flask = Flask(__name__)

if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

    @app_flask.route("/callback", methods=['POST'])
    def callback():
        signature = request.headers['X-Line-Signature']
        body = request.get_data(as_text=True)
        try: handler.handle(body, signature)
        except InvalidSignatureError: abort(400)
        return 'OK'

    @handler.add(MessageEvent, message=TextMessage)
    def handle_line_message(event):
        text = event.message.text
        user_id = event.source.user_id
        # LINE entry point
        resp = process_command(text, user_id, user_id, "line")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=resp))

def run_flask():
    app_flask.run(host='0.0.0.0', port=5000, use_reloader=False)

# =========================================
#       MAIN EXECUTION
# =========================================
if __name__ == '__main__':
    # Flask Thread (LINE)
    if LINE_CHANNEL_ACCESS_TOKEN:
        logger.info("🟢 Starting LINE Bot (Flask)...")
        t = threading.Thread(target=run_flask)
        t.daemon = True
        t.start()
    
    # Telegram Polling (Main Thread)
    if TELEGRAM_TOKEN:
        logger.info("🔵 Starting Telegram Bot...")
        app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        
        # Commands
        for cmd in ['start', 'help', 'add', 'delete', 'update', 'today', 'week', 'spend', 'report', 'stock', 'weather', 's', 'remind']:
            app_tg.add_handler(CommandHandler(cmd, t_cmd_wrapper))
            
        # Messages (AI Router)
        app_tg.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_msg_handler))
        
        app_tg.run_polling()
    else:
        logger.error("❌ Telegram Token missing! App might exit.")
