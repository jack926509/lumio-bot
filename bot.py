
import logging
import os
import requests
import json
import sqlite3
import datetime
import traceback
import re
from datetime import date as dt_date

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

import openai
from dotenv import load_dotenv
import yfinance as yf
from duckduckgo_search import DDGS

# --- Google Integration ---
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build

# --- Configuration ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_JSON = "google_secret.json"
SPREADSHEET_NAME = "MyExpenses"
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Set API Keys
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
else:
    print("⚠️ Warning: OPENAI_API_KEY is missing!")

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Database Setup ---
DB_FILE = 'assistant.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Todos Table
    c.execute('''CREATE TABLE IF NOT EXISTS todos (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, 
                 user_id INTEGER, 
                 task TEXT, 
                 category TEXT DEFAULT 'general', 
                 status TEXT DEFAULT 'pending', 
                 created_at TEXT)''')
    # Reminders Table
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, 
                 user_id INTEGER, 
                 chat_id INTEGER, 
                 remind_time TEXT, 
                 task TEXT, 
                 status TEXT DEFAULT 'pending')''')
    # Notes Table
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, 
                 user_id INTEGER, 
                 content TEXT, 
                 created_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# =========================================
#       HELPER FUNCTIONS (Utility)
# =========================================

# --- Google Credentials ---
def get_google_creds():
    env_json = os.getenv("GOOGLE_JSON_KEY")
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']
    
    # Priority 1: Env Var
    if env_json:
        try:
            return ServiceAccountCredentials.from_json_keyfile_dict(json.loads(env_json), scope)
        except Exception as e:
            print(f"Env JSON Error: {e}")
    
    # Priority 2: Local File
    if os.path.exists(GOOGLE_SHEET_JSON):
        return ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEET_JSON, scope)
    
    return None

# --- Accounting ---
def add_to_google_sheet(date, category, amount, note):
    try:
        creds = get_google_creds()
        if not creds: return False
        
        client = gspread.authorize(creds)
        
        # Try finding sheet, fallback to create or default
        try: 
            sh = client.open(SPREADSHEET_NAME)
        except gspread.SpreadsheetNotFound:
            return False

        try: 
            sheet = sh.worksheet("records")
        except: 
            sheet = sh.sheet1
        
        # Ensure Headers
        try:
             if sheet.cell(1, 1).value != '日期': 
                 sheet.insert_row(['日期', '項目', '金額', '備註'], 1)
        except: pass
            
        sheet.append_row([date, category, amount, note])
        return True
    except Exception as e:
        print(f"Sheet Error: {e}")
        return False

def get_monthly_report():
    try:
        creds = get_google_creds()
        if not creds: return "❌ 無法連接 Google Sheets (憑證錯誤)"
        
        client = gspread.authorize(creds)
        try:
            sheet = client.open(SPREADSHEET_NAME).worksheet("records")
        except:
            return "❌ 找不到 'records' 工作表"

        data = sheet.get_all_records()
        
        current_month = datetime.datetime.now().strftime("%Y-%m")
        total = 0
        cat_total = {}
        
        for row in data:
            if current_month in str(row['日期']):
                try:
                    amt = float(row.get('金額', 0))
                except: amt = 0
                cat = row.get('項目', '其他')
                total += amt
                cat_total[cat] = cat_total.get(cat, 0) + amt
        
        if total == 0: return f"📊 本月 ({current_month}) 尚無支出紀錄。"
        
        msg = f"📊 **本月 ({current_month}) 支出報表**\n\n"
        msg += f"💰 **總支出：${total:,.0f}**\n\n"
        msg += "**分類統計：**\n"
        for cat, amt in cat_total.items():
            msg += f"- {cat}: ${amt:,.0f}\n"
        return msg
    except Exception as e: return f"❌ 報表產生失敗: {e}"

# --- Calendar ---
def get_cal_service():
    creds = get_google_creds()
    if not creds: return None
    return build('calendar', 'v3', credentials=creds)

def add_event(text):
    try:
        service = get_cal_service()
        if not service: return "❌ 未設定 Google Calendar"
        
        prompt = f"""
        Extract event from '{text}'. 
        Return ONLY valid JSON. No markdown.
        Format: {{"summary": "Name", "start_time": "ISO8601 (Local Time)", "duration_minutes": 60}}
        Ref Date: {datetime.datetime.now().strftime('%Y-%m-%d')}
        timezone: Asia/Taipei
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        content = res.choices[0].message.content.strip()
        
        # Clean markdown wrappers if present
        if content.startswith("```"):
            content = re.sub(r'^```json\s*|^```\s*|```$', '', content, flags=re.MULTILINE).strip()

        try:
            js = json.loads(content)
        except:
            return f"❌ 無法解析 AI 回應: {content}"
        
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
        traceback.print_exc()
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
        
        if not events: return f"📅 未來 {days} 天內沒有行程。"
        
        msg = f"📅 **未來 {days} 天行程**:\n"
        for e in events:
            # Time handling
            start = e['start'].get('dateTime')
            if start:
                dt = datetime.datetime.fromisoformat(start)
                time_str = dt.strftime('%m/%d %H:%M')
            else:
                start = e['start'].get('date')
                dt = datetime.datetime.strptime(start, '%Y-%m-%d')
                time_str = dt.strftime('%m/%d (全天)')
            
            weekdays = ["一","二","三","四","五","六","日"]
            wd = weekdays[dt.weekday()]
            
            msg += f"• {time_str} ({wd}) {e['summary']}\n"
        return msg
    except Exception as e:
        traceback.print_exc() 
        return "❌ 讀取失敗"

# --- Calendar Delete / Update Helpers ---
def find_event_by_query(query):
    # Search logic with fuzzy matching
    print(f"DEBUG: Searching for '{query}'")
    try:
        service = get_cal_service()
        if not service: return None, "❌ Google Calendar Service Failed"
        
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now, maxResults=50, singleEvents=True, orderBy='startTime').execute().get('items', [])
        
        # Clean query: remove parens
        clean_query = re.sub(r'\s*\(.*?\)', '', query).strip()
        print(f"DEBUG: Cleaned Query -> '{clean_query}'")
        
        matches = []
        for e in events:
            summary = e['summary'].lower()
            q_lower = clean_query.lower()
            
            if q_lower in summary:
                matches.append(e)
            elif summary in q_lower and len(summary) > 1:
                matches.append(e)
        
        if not matches:
             msg = f"❌ 找不到包含 '{clean_query}' 的近期行程。\n建議：\n"
             for e in events[:5]:
                 msg += f"- {e['summary']}\n"
             return None, msg
        
        # If multiple, prefer exact match
        exacts = [e for e in matches if e['summary'].lower() == clean_query.lower()]
        if len(exacts) == 1:
            return exacts[0], None

        return matches[0], None
    except Exception as e:
        return None, str(e)

def delete_event(query):
    target, error_msg = find_event_by_query(query)
    if error_msg: return error_msg
    
    try:
        service = get_cal_service()
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=target['id']).execute()
        return f"🗑️ 已刪除行程: {target['summary']}"
    except Exception as e: 
        return f"❌ 刪除失敗: {e}"

def update_event(query):
    try:
        service = get_cal_service()
        
        # AI Analysis
        prompt = f"""
        User wants to update event. Input: '{query}'
        Return JSON: {{"target_keywords": "string", "new_instruction": "string"}}
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        js = json.loads(res.choices[0].message.content.strip().replace('`json','').replace('`',''))
        
        target_kw = js['target_keywords']
        instruction = js['new_instruction']
        
        target_event, error_msg = find_event_by_query(target_kw)
        if error_msg: return error_msg
        
        prompt_update = f"""
        Update this event: '{target_event['summary']}' (Time: {target_event['start'].get('dateTime')})
        Instruction: '{instruction}'
        Return valid JSON: {{"summary": "New Name", "start_time": "ISO8601", "duration_minutes": 60}}
        Ref Date: {datetime.datetime.now().strftime('%Y-%m-%d')}
        """
        res_up = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt_update}])
        js_up = json.loads(res_up.choices[0].message.content.strip().replace('`json','').replace('`',''))
        
        start = datetime.datetime.fromisoformat(js_up['start_time'])
        end = start + datetime.timedelta(minutes=js_up.get('duration_minutes', 60))
        
        body = {
            'summary': js_up.get('summary', target_event['summary']),
            'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
            'end': {'dateTime': end.isoformat(), 'timeZone': 'Asia/Taipei'},
        }
        
        service.events().patch(calendarId=GOOGLE_CALENDAR_ID, eventId=target_event['id'], body=body).execute()
        return f"🔄 已更新: {body['summary']} ({start.strftime('%m/%d %H:%M')})"
        
    except Exception as e:
        traceback.print_exc()
        return f"❌ 更新失敗: {e}"

# --- Weather ---
def get_weather(location="Taipei"):
    try:
        url = f"https://wttr.in/{location}?format=%l:+%c+%t+(%h)"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return res.text.strip()
        return "無法取得天氣資訊"
    except: return "無法取得天氣資訊"

# --- Stock ---
def get_stock(symbol):
    try:
        if not symbol: return "請輸入代號 (例如 /stock TSLA)"
        symbol = symbol.upper()
        ticker = yf.Ticker(symbol)
        
        hist = ticker.history(period="1d")
        if hist.empty: return f"❌ 找不到 {symbol}"
        price = hist['Close'].iloc[-1]
        
        # News
        news_summary = ""
        try:
            news = ticker.news
            if news: news_summary = "\n".join([n['title'] for n in news[:3]])
        except: pass
        
        # AI Analysis
        prompt = f"""
        Stock: {symbol} (${price:.2f}). 
        News: {news_summary}
        Role: Lumio (Sweet Girlfriend + Analyst).
        Task: Short bullish/bearish analysis in Traditional Chinese.
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        analysis = res.choices[0].message.content
        
        return f"📈 **{symbol}**: ${price:.2f}\n\n{analysis}"
    except Exception as e:
        return f"❌ 查詢失敗: {e}"

# --- Search ---
def search_web(q):
    try:
        res = DDGS().text(q, max_results=3)
        if not res: return "❌ 搜尋不到結果"
        return "\n".join([f"- [{r['title']}]({r['href']})" for r in res])
    except: return "❌ 搜尋機制暫時無法使用"

# --- AI Chat ---
def ai_chat(text):
    try:
        weather_context = ""
        if "天氣" in text or "weather" in text.lower():
            w_data = get_weather("Taipei")
            weather_context = f" [Current Taipei Weather: {w_data}]"

        system_prompt = f"""
        You are Lumio (盧米奧), the user's loving girlfriend.
        Personality: Sweet, caring, encouraging, uses emojis (❤️, 😘).
        Language: Traditional Chinese (Taiwan).
        Context: Helps with life/finance/schedule.{weather_context}
        """
        
        res = openai.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ]
        )
        return res.choices[0].message.content
    except Exception as e:
        print(f"AI Chat Error: {e}")
        return "嗚嗚... 親愛的我的腦袋有點卡住了 🥺"

# =========================================
#       COMMAND HANDLERS
# =========================================

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE): 
    await u.message.reply_text("👋 Lumio V8.0 重構重生版！親愛的久等了 ❤️")

async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = """
🤖 **Lumio 指令大全**
📅 `/add`, `/delete`, `/update` - 行程管理
📅 `/today`, `/week` - 查詢行程
💰 `/spend`, `/report` - 記帳
⏰ `/remind 10分鐘後 喝水` - 提醒
📝 `/todo`, `/done`, `/note` - 待辦與筆記
🌍 `/weather 台北` - 天氣
📈 `/stock AAPL` - 股價分析
🔍 `/s 關鍵字` - 搜尋
    """
    await u.message.reply_text(msg, parse_mode='Markdown')

# Calendar Handlers
async def add_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(add_event(' '.join(c.args)))
async def del_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(delete_event(' '.join(c.args)))
async def update_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(update_event(' '.join(c.args)))
async def today_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(list_events(1))
async def week_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(list_events(7))

# Accounting Handlers
async def spend_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(c.args[0])
        cat = c.args[1]
        note = ' '.join(c.args[2:])
        if add_to_google_sheet(dt_date.today().isoformat(), cat, amt, note):
            await u.message.reply_text(f"💸 已記帳: {cat} ${amt}")
        else: await u.message.reply_text("❌ 記帳失敗")
    except: await u.message.reply_text("格式: /spend 100 午餐")

async def report_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE): await u.message.reply_text(get_monthly_report(), parse_mode='Markdown')

# Todo & Note Handlers
async def todo_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO todos (user_id, task) VALUES (?, ?)", (u.effective_user.id, ' '.join(c.args)))
    conn.commit(); conn.close()
    await u.message.reply_text("✅ 待辦 +1")

async def list_todos(u: Update, c: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT id, task FROM todos WHERE user_id=? AND status='pending'", (u.effective_user.id,)).fetchall()
    conn.close()
    msg = "📋 **待辦清單**\n" + "\n".join([f"{r[0]}. {r[1]}" for r in rows]) if rows else "🎉 無待辦事項"
    await u.message.reply_text(msg, parse_mode='Markdown')

async def done_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("UPDATE todos SET status='done' WHERE id=?", (c.args[0],))
        conn.commit(); conn.close()
        await u.message.reply_text("✅ 完成！")
    except: pass

async def note_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(c.args)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO notes (user_id, content, created_at) VALUES (?, ?, ?)", 
                 (u.effective_user.id, text, datetime.datetime.now().strftime('%Y-%m-%d')))
    conn.commit(); conn.close()
    await u.message.reply_text("📝 筆記已儲存")

# Reminder Handlers
async def remind_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(c.args)
    if not text: return await u.message.reply_text("例: /remind 10分鐘後 關瓦斯")
    try:
        prompt = f"""
        Extract time and task from: "{text}". Current: {datetime.datetime.now()}
        Return JSON: {{"time": "YYYY-MM-DD HH:MM:SS", "task": "string"}}
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        js = json.loads(res.choices[0].message.content.strip().replace('`json','').replace('`',''))
        
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO reminders (user_id, chat_id, remind_time, task) VALUES (?, ?, ?, ?)", 
                     (u.effective_user.id, u.effective_chat.id, js['time'], js['task']))
        conn.commit(); conn.close()
        await u.message.reply_text(f"✅ 提醒已設定: {js['task']} ({js['time']})")
    except Exception as e: await u.message.reply_text(f"❌ 失敗: {e}")

async def list_reminders(u: Update, c: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT id, remind_time, task FROM reminders WHERE status='pending' ORDER BY remind_time ASC").fetchall()
    conn.close()
    msg = "⏰ **提醒清單**:\n" + "\n".join([f"{r[1]}: {r[2]}" for r in rows]) if rows else "🎉 無提醒"
    await u.message.reply_text(msg, parse_mode='Markdown')

async def check_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE)
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows = conn.execute("SELECT id, chat_id, task, remind_time FROM reminders WHERE status='pending' AND remind_time <= ?", (now,)).fetchall()
        for row in rows:
            await context.bot.send_message(chat_id=row[1], text=f"🔔 **提醒**\n{row[2]}")
            conn.execute("UPDATE reminders SET status='sent' WHERE id=?", (row[0],))
        conn.commit(); conn.close()
    except Exception as e: print(f"Job Error: {e}")

# Info Handlers
async def weather_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(get_weather(c.args[0] if c.args else 'Taipei'))

async def stock_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(get_stock(c.args[0] if c.args else ''), parse_mode='Markdown')

async def s_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(search_web(' '.join(c.args)), parse_mode='Markdown')

async def debug(u: Update, c: ContextTypes.DEFAULT_TYPE):
    creds = get_google_creds()
    await u.message.reply_text(f"Connection Status: {'✅ OK' if creds else '❌ Failed'}")

async def msg_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.message.text: await u.message.reply_text(ai_chat(u.message.text))

# =========================================
#       MAIN EXECUTION
# =========================================

if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not found!")
        exit(1)
        
    print("🤖 Starting Lumio V8.0...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('delete', del_cmd))
    app.add_handler(CommandHandler('update', update_cmd))
    app.add_handler(CommandHandler('today', today_cmd))
    app.add_handler(CommandHandler('week', week_cmd))
    
    app.add_handler(CommandHandler('spend', spend_cmd))
    app.add_handler(CommandHandler('report', report_cmd))
    
    app.add_handler(CommandHandler('remind', remind_cmd))
    app.add_handler(CommandHandler('reminders', list_reminders))
    
    app.add_handler(CommandHandler('todo', todo_cmd))
    app.add_handler(CommandHandler('todos', list_todos))
    app.add_handler(CommandHandler('done', done_cmd))
    app.add_handler(CommandHandler('note', note_cmd))
    
    app.add_handler(CommandHandler('weather', weather_cmd))
    app.add_handler(CommandHandler('stock', stock_cmd))
    app.add_handler(CommandHandler('s', s_cmd))
    app.add_handler(CommandHandler('debug', debug))
    
    # Chat Handler (Must be last)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), msg_handler))
    
    # Job Queue
    if app.job_queue:
        app.job_queue.run_repeating(check_reminders_job, interval=60, first=10)
        print("✅ Job Queue Started")
    
    print("🚀 Lumio is Online!")
    app.run_polling()
