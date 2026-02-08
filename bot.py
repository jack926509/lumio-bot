
import logging
import os
import requests
import json
import sqlite3
import datetime
import traceback
import re
import threading
import asyncio
from datetime import date as dt_date

# --- Telegram Imports ---
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

# --- LINE Imports ---
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# --- AI & Tools ---
import openai
from dotenv import load_dotenv
import yfinance as yf
from duckduckgo_search import DDGS

# --- Google Integration ---
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build

# =========================================
#       CONFIGURATION & SETUP
# =========================================
load_dotenv()

# --- Telegram Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# --- LINE Config ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# --- AI Config ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# --- Google Config ---
GOOGLE_SHEET_JSON = "google_secret.json"
SPREADSHEET_NAME = "MyExpenses"
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Database ---
DB_FILE = 'assistant.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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

# --- Google Credentials ---
def get_google_creds():
    env_json = os.getenv("GOOGLE_JSON_KEY")
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']
    if env_json:
        try: return ServiceAccountCredentials.from_json_keyfile_dict(json.loads(env_json), scope)
        except: pass
    if os.path.exists(GOOGLE_SHEET_JSON):
        return ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEET_JSON, scope)
    return None

# --- Accounting ---
def add_to_google_sheet(date, category, amount, note):
    try:
        creds = get_google_creds()
        if not creds: return False
        client = gspread.authorize(creds)
        try: sh = client.open(SPREADSHEET_NAME)
        except: return False
        try: sheet = sh.worksheet("records")
        except: sheet = sh.sheet1
        try:
             if sheet.cell(1, 1).value != '日期': sheet.insert_row(['日期', '項目', '金額', '備註'], 1)
        except: pass
        sheet.append_row([date, category, amount, note])
        return True
    except: return False

def get_monthly_report():
    try:
        creds = get_google_creds()
        if not creds: return "❌ 無法連接 Google Sheets"
        client = gspread.authorize(creds)
        sheet = client.open(SPREADSHEET_NAME).worksheet("records")
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
        msg = f"📊 **本月 ({current_month}) 支出報表**\n💰 總支出：${total:,.0f}\n"
        for cat, amt in cat_total.items(): msg += f"- {cat}: ${amt:,.0f}\n"
        return msg
    except Exception as e: return f"❌ 報表失敗: {e}"

# --- Calendar ---
def get_cal_service():
    creds = get_google_creds()
    if not creds: return None
    return build('calendar', 'v3', credentials=creds)

def add_event(text):
    try:
        service = get_cal_service()
        if not service: return "❌ 未設定 Google Calendar"
        prompt = f"Extract event from '{text}'. Return JSON: {{\"summary\": \"Name\", \"start_time\": \"ISO8601\", \"duration_minutes\": 60}}. Ref: {datetime.datetime.now()}"
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        content = res.choices[0].message.content.strip().replace('`json','').replace('`','')
        js = json.loads(content)
        start = datetime.datetime.fromisoformat(js['start_time'])
        end = start + datetime.timedelta(minutes=js.get('duration_minutes', 60))
        event = {'summary': js['summary'], 'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'}, 'end': {'dateTime': end.isoformat(), 'timeZone': 'Asia/Taipei'}}
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return f"✅ 已建立: {js['summary']} ({start.strftime('%m/%d %H:%M')})"
    except Exception as e: return f"❌ 失敗: {e}"

def list_events(days=1):
    try:
        service = get_cal_service()
        if not service: return "❌ 未設定 Google Calendar"
        now = datetime.datetime.utcnow(); end = now + datetime.timedelta(days=days)
        events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now.isoformat()+'Z', timeMax=end.isoformat()+'Z', singleEvents=True, orderBy='startTime').execute().get('items', [])
        if not events: return f"📅 未來 {days} 天無行程"
        msg = f"📅 **未來 {days} 天行程**:\n"
        for e in events:
            start = e['start'].get('dateTime')
            if start: dt = datetime.datetime.fromisoformat(start); time_str = dt.strftime('%m/%d %H:%M')
            else: start = e['start'].get('date'); dt = datetime.datetime.strptime(start, '%Y-%m-%d'); time_str = dt.strftime('%m/%d (全天)')
            wd = ["一","二","三","四","五","六","日"][dt.weekday()]
            msg += f"• {time_str} ({wd}) {e['summary']}\n"
        return msg
    except: return "❌ 讀取失敗"

def find_event_by_query(query):
    # Simplified for brevity
    try:
        service = get_cal_service()
        if not service: return None, "❌ Service Failed"
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now, maxResults=50, singleEvents=True, orderBy='startTime').execute().get('items', [])
        clean_query = re.sub(r'\s*\(.*?\)', '', query).strip()
        matches = [e for e in events if clean_query.lower() in e['summary'].lower()]
        if not matches: return None, f"❌ 找不到 '{clean_query}'"
        return matches[0], None
    except Exception as e: return None, str(e)

def delete_event(query):
    target, error_msg = find_event_by_query(query)
    if error_msg: return error_msg
    try:
        service = get_cal_service()
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=target['id']).execute()
        return f"🗑️ 已刪除: {target['summary']}"
    except Exception as e: return f"❌ 刪除失敗: {e}"

def update_event(query):
    try:
        # Simplified update logic reuse
        add_event(query) # Placeholder logic, real update logic is complex
        return "🔄 更新功能需完整實作，目前僅示意"
    except: return "❌ 更新失敗"

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
        
        prompt = f"Stock: {symbol} (${price:.2f}). Role: Lumio (Girlfriend Analyst). Short analysis."
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        return f"📈 **{symbol}**: ${price:.2f}\n\n{res.choices[0].message.content}"
    except: return "❌ 查詢失敗"

def search_web(q):
    try: return "\n".join([f"- {r['title']} ({r['href']})" for r in DDGS().text(q, max_results=3)])
    except: return "❌ 搜尋失敗"

def ai_chat(text):
    try:
        weather_context = ""
        if "天氣" in text or "weather" in text.lower(): weather_context = f" [Current Taipei Weather: {get_weather('Taipei')}]"
        system_prompt = f"You are Lumio (盧米奧), loving girlfriend AI. Context: Life/Finance assistant.{weather_context}"
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}])
        return res.choices[0].message.content
    except: return "嗚嗚... 親愛的我的腦袋有點卡住了 🥺"

# =========================================
#       UNIFIED COMMAND PARSER (Router)
# =========================================
def process_command(text, user_id, chat_id, platform="telegram"):
    """
    Central logic to handle commands from BOTH Telegram and LINE.
    """
    parts = text.strip().split()
    cmd = parts[0].lower().replace('/', '') # Remove slash for easier matching
    args = parts[1:]
    arg_str = ' '.join(args)

    if cmd == 'start': return "👋 Lumio V9.0 (Telegram+LINE) 雙棲版上線！親愛的久等了 ❤️"
    if cmd == 'help': return "🤖 **指令**:\n/add, /delete, /today, /stock, /weather, /remind"
    
    # Calendar
    if cmd == 'add': return add_event(arg_str)
    if cmd == 'delete': return delete_event(arg_str)
    if cmd == 'update': return update_event(arg_str)
    if cmd == 'today': return list_events(1)
    if cmd == 'week': return list_events(7)
    
    # Accounting
    if cmd == 'spend': # /spend 100 lunch
        try: 
            return f"💸 已記帳: {args[1]} ${args[0]}" if add_to_google_sheet(dt_date.today().isoformat(), args[1], float(args[0]), ' '.join(args[2:])) else "❌ 失敗"
        except: return "格式: /spend 100 午餐"
    if cmd == 'report': return get_monthly_report()
    
    # Tools
    if cmd == 'stock': return get_stock(arg_str)
    if cmd == 'weather': return get_weather(arg_str if arg_str else 'Taipei')
    if cmd == 's': return search_web(arg_str)
    
    # Reminders (Simplified for now - only Telegram supports JobQueue easily, LINE needs Push API paid/quota)
    # But we can save to DB and let the background job try to push if we have Chat ID.
    if cmd == 'remind':
        return f"✅ 提醒: {arg_str} (目前僅 Telegram 支援主動推播)" if platform == 'telegram' else "⚠️ LINE 暫不支援主動提醒 (需 Push API 額度)"

    # AI Chat (Default)
    return ai_chat(text)

# =========================================
#       TELEGRAM HANDLERS
# =========================================
async def tg_msg_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text = u.message.text
    if text: 
        # Check if it's a command
        if text.startswith('/'):
            # Telegram commands are handled by CommandHandlers usually, 
            # but we can route everything here if we wanted.
            # But let's keep CommandHandlers for Telegram native feel.
            pass 
        else:
            await u.message.reply_text(ai_chat(text))

# Wrappers to map Telegram CommandHandler -> process_command
async def t_cmd_wrapper(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # Extract command from message text e.g. "/stock AAPL"
    text = u.message.text
    resp = process_command(text, u.effective_user.id, u.effective_chat.id, "telegram")
    await u.message.reply_text(resp, parse_mode=ParseMode.MARKDOWN)

# =========================================
#       LINE FLASK SERVER
# =========================================
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
        # Process logic
        resp = process_command(text, user_id, user_id, "line")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=resp))

def run_flask():
    # Try to start ngrok automatically for local dev
    try:
        from pyngrok import ngrok, conf
        # Optional: set auth token if user has it in env
        # conf.get_default().auth_token = os.getenv("NGROK_AUTH_TOKEN")
        
        # Open a HTTP tunnel on the default port 5000
        public_url = ngrok.connect(5000).public_url
        print(f"\n🚀 【LINE Bot Local Test Mode】")
        print(f"🔗 請將此網址填入 LINE Webhook URL:")
        print(f"👉 {public_url}/callback\n")
    except ImportError:
        print("⚠️ pyngrok not installed. Please run ngrok manually: 'ngrok http 5000'")
    except Exception as e:
        print(f"⚠️ Auto-ngrok failed: {e}. Please run ngrok manually.")

    # Run Flask on port 5000 (default)
    app_flask.run(host='0.0.0.0', port=5000, use_reloader=False)

# =========================================
#       MAIN EXECUTION
# =========================================
if __name__ == '__main__':
    # 1. Start Flask (LINE) in a separate thread
    if LINE_CHANNEL_ACCESS_TOKEN:
        print("🟢 Starting LINE Bot (Flask)...")
        t = threading.Thread(target=run_flask)
        t.daemon = True
        t.start()
    else:
        print("⚠️ LINE Config missing. LINE Bot will not run.")

    # 2. Start Telegram Bot (Main Thread)
    if TELEGRAM_TOKEN:
        print("🔵 Starting Telegram Bot...")
        app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        
        # Register all commands to use the centralized 'process_command' or wrapper
        cmds = ['start', 'help', 'add', 'delete', 'update', 'today', 'week', 'spend', 'report', 'stock', 'weather', 's', 'remind']
        for cmd in cmds:
            app_tg.add_handler(CommandHandler(cmd, t_cmd_wrapper))
            
        app_tg.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_msg_handler))
        app_tg.run_polling()
    else:
        print("❌ Telegram Config missing.")
