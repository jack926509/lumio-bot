
import logging
import os
import json
import sqlite3
import datetime
import traceback
import re
import threading
import requests
from datetime import date as dt_date, timedelta, timezone

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

# Robust Import for Google Search Fallback
try:
    from googlesearch import search as g_search
except ImportError:
    g_search = None

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build

# =========================================
#       CONFIGURATION & SETUP (V13.0)
# =========================================
load_dotenv()

# --- Timezone Setup (UTC+8) ---
TZ_TAIPEI = timezone(timedelta(hours=8))

def get_now():
    return datetime.datetime.now(TZ_TAIPEI)

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

# =========================================
#       CORE LOGIC
# =========================================

# --- Google Credentials ---
def get_google_creds():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/calendar']
    
    # Priority 1: Env Var (Cloud)
    if GOOGLE_JSON_KEY:
        try:
            cleaned_json = GOOGLE_JSON_KEY.replace('\\n', '\n')
            creds_dict = json.loads(cleaned_json, strict=False)
            return ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        except Exception as e:
            logger.error(f"Google Env Key Error: {e}")
    
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
        try: sh = client.open(SPREADSHEET_NAME)
        except: return False

        try: sheet = sh.worksheet("records")
        except: sheet = sh.sheet1
        
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
        current_month = get_now().strftime("%Y-%m")
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
        Extract event from: '{text}'. 
        Return ONLY valid JSON.
        Format: {{"summary": "Name", "start_time": "ISO8601 (Local Time)", "duration_minutes": 60}}
        Ref Date: {get_now().strftime('%Y-%m-%d')}
        timezone: Asia/Taipei
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        content = res.choices[0].message.content.strip()
        
        start_idx = content.find('{')
        end_idx = content.rfind('}')
        if start_idx != -1 and end_idx != -1:
            js = json.loads(content[start_idx : end_idx + 1])
        else:
            return f"❌ AI 無法理解: {content[:50]}"
            
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
            calendarId=GOOGLE_CALENDAR_ID, timeMin=now.isoformat()+'Z', timeMax=end.isoformat()+'Z', 
            singleEvents=True, orderBy='startTime'
        ).execute().get('items', [])
        
        if not events: return f"📅 未來 {days} 天無行程"
        msg = f"📅 **未來 {days} 天行程**:\n"
        for e in events:
            start = e['start'].get('dateTime') or e['start'].get('date')
            try:
                dt = datetime.datetime.fromisoformat(start)
            except:
                dt = datetime.datetime.strptime(start, '%Y-%m-%d')
            
            wd = ["一","二","三","四","五","六","日"][dt.weekday()]
            time_str = dt.strftime('%m/%d %H:%M') if 'T' in start else dt.strftime('%m/%d (全天)')
            msg += f"• {time_str} ({wd}) {e['summary']}\n"
        return msg
    except Exception as e: return f"❌ 讀取失敗: {e}"

def delete_event(query):
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
    return "🔄 建議直接刪除後重新建立"

# --- Tools ---
def get_weather(location="Taipei"):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(f"https://wttr.in/{location}?format=%l:+%c+%t+(%h)", headers=headers, timeout=5)
        if r.status_code == 200: return r.text.strip()
        return "⚠️ 暫時無法取得天氣"
    except: return "❌ 連線失敗"

def get_stock(symbol):
    try:
        if not symbol: return "請輸入代號"
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period="5d") # Get enough days
        if hist.empty: return f"❌ 找不到 {symbol}"
        
        price = hist['Close'].iloc[-1]
        try:
            prev_close = hist['Close'].iloc[-2]
            change = price - prev_close
            pct = (change / prev_close) * 100
            arrow = "🔺" if change > 0 else "🔻" if change < 0 else "➖"
            sign = "+" if change > 0 else ""
            status_str = f"{arrow} ${price:.2f} ({sign}{change:.2f} / {sign}{pct:.2f}%)"
        except:
            status_str = f"${price:.2f}"
            
        prompt = f"""
        Stock: {symbol} ({status_str}). 
        Role: Financial Analyst (Traditional Chinese).
        Task: Short analysis (max 80 words).
        """
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
        return f"📈 **{symbol}**: {status_str}\n\n{res.choices[0].message.content}"
    except: return "❌ 查詢失敗"

def search_web(q):
    results = []
    # 1. DuckDuckGo
    try:
        with DDGS() as ddgs:
            gen = ddgs.text(q, max_results=3)
            if gen: results = [f"- [{r['title']}]({r['href']})" for r in gen]
    except Exception as e: logger.error(f"DDG: {e}")
    
    # 2. Google Fallback
    if not results and g_search:
        try:
            logger.info("🔄 Google Fallback...")
            for r in g_search(q, num_results=3, advanced=True):
                t = getattr(r, 'title', r.url)
                l = getattr(r, 'url', str(r))
                results.append(f"- [{t}]({l})")
        except Exception as e: logger.error(f"Google: {e}")

    # 3. GPT-4o Fallback
    if not results:
        try:
            prompt = f"Search '{q}' failed. Provide a short summary based on knowledge."
            res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
            return f"⚠️ 搜尋無回應，AI 補充:\n\n{res.choices[0].message.content}"
        except: return "❌ 搜尋功能暫時失效"
        
    return "🔍 **搜尋結果**:\n" + "\n".join(results)

def ai_chat(text):
    try:
        now = get_now()
        time_str = now.strftime('%Y-%m-%d %H:%M')
        weekday = ["一","二","三","四","五","六","日"][now.weekday()]
        
        weather_context = ""
        if "天氣" in text or "weather" in text.lower(): 
            weather_context = f" [Taipei Weather: {get_weather('Taipei')}]"
            
        system_prompt = f"""
        You are Lumio (盧米奧), an advanced AI assistant with a sweet personality.
        🕒 Time: {time_str} (週{weekday}) | Location: Taipei {weather_context}
        
        🎯 **MODES**:
        1. **❤️ Sweet Girlfriend** (Default): Chat, daily life, feelings. Use emojis.
        2. **🧠 Professional Assistant** (Tasks): Edit, Translate, Analyze. Be precise, less emojis.
        
        🌍 Language: Traditional Chinese (Taiwan).
        """
        res = openai.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.7
        )
        return res.choices[0].message.content
    except: return "腦袋運轉中... 請稍後再試 🥺"

# =========================================
#       ROUTER
# =========================================
def process_command(text, user_id, chat_id, platform="telegram"):
    if not text: return ""
    
    # Simple Commands
    if text.startswith('/'):
        parts = text.strip().split()
        cmd = parts[0].lower().replace('/', '')
        arg_str = ' '.join(parts[1:])
        
        if cmd == 'start': return "👋 Lumio V13.0 全能型上線！"
        if cmd == 'help': return "🤖 指令: /add, /today, /stock, /weather"
        if cmd == 'add': return add_event(arg_str)
        if cmd == 'delete': return delete_event(arg_str)
        if cmd == 'update': return update_event(arg_str)
        if cmd == 'today': return list_events(1)
        if cmd == 'week': return list_events(7)
        if cmd == 'spend': 
            try: return f"💸 已記帳: {parts[2]} ${parts[1]}" if add_to_google_sheet(get_now().strftime('%Y-%m-%d'), parts[2], float(parts[1]), ' '.join(parts[3:])) else "❌ 失敗"
            except: return "格式: /spend 100 午餐"
        if cmd == 'report': return get_monthly_report()
        if cmd == 'stock': return get_stock(arg_str)
        if cmd == 'weather': return get_weather(arg_str or 'Taipei')
        if cmd == 's': return search_web(arg_str)

    # Natural Language Router
    system_prompt = """
    Classify intent:
    - ADD_EVENT (e.g. "新增行程", "明天開會")
    - DELETE_EVENT (e.g. "取消開會")
    - LIST_EVENTS (e.g. "今天行程", "一週行程")
    - SPEND (e.g. "記帳 午餐 150", "花費")
    - REPORT (e.g. "報表")
    - STOCK (e.g. "台積電股價", "2330", "查詢AAPL")
    - WEATHER (e.g. "天氣")
    - SEARCH (e.g. "搜尋", "查一下")
    - CHAT (Default)

    Return JSON: {"intent": "INTENT", "args": "content"}
    Rules:
    - SPEND: "amount category [note]" (Amount First!)
    - STOCK: SYMBOL ONLY (Remove '查詢', 'price').
    - DELETE: Keywords ONLY.
    - SEARCH: Keywords ONLY.
    """
    try:
        res = openai.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}], temperature=0)
        content = res.choices[0].message.content.strip()
        start = content.find('{'); end = content.rfind('}')
        if start != -1:
            js = json.loads(content[start:end+1])
            intent = js.get('intent', 'CHAT')
            args = js.get('args', text)
        else: intent = 'CHAT'; args = text
        
        # Safety Nets
        if intent == 'CHAT' and ('記帳' in text or 'spend' in text.lower()): intent = 'SPEND'; args = text

        if intent == 'ADD_EVENT': return add_event(text)
        if intent == 'DELETE_EVENT': return delete_event(args)
        if intent == 'LIST_EVENTS': return list_events(7 if any(k in args for k in ['7', '七', 'week', '週']) else 1)
        if intent == 'SPEND':
            # Extract number
            try:
                nums = re.findall(r'\d+(?:\.\d+)?', args)
                if nums:
                    amt = float(nums[0])
                    clean = re.sub(r'\d+(?:\.\d+)?|記帳|spend', '', args, flags=re.IGNORECASE).strip() or "雜支"
                    if add_to_google_sheet(get_now().strftime('%Y-%m-%d'), clean, amt, text): return f"💸 已記帳: {clean} ${amt}"
            except: pass
            return "❌ 記帳失敗 (例: 記帳 100 午餐)"
            
        if intent == 'REPORT': return get_monthly_report()
        if intent == 'STOCK': return get_stock(args)
        if intent == 'WEATHER': return get_weather(args)
        if intent == 'SEARCH': return search_web(args)

        return ai_chat(text)
    except: return ai_chat(text)

# =========================================
#       HANDLERS
# =========================================
async def t_cmd_wrapper(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(process_command(u.message.text, u.effective_user.id, u.effective_chat.id, "telegram"), parse_mode=ParseMode.MARKDOWN)

async def tg_msg_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.message.text and not u.message.text.startswith('/'):
        await u.message.reply_text(process_command(u.message.text, u.effective_user.id, u.effective_chat.id, "telegram"))

# LINE Flask
app_flask = Flask(__name__)
if LINE_CHANNEL_ACCESS_TOKEN:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

    @app_flask.route("/callback", methods=['POST'])
    def callback():
        try: handler.handle(request.get_data(as_text=True), request.headers['X-Line-Signature'])
        except InvalidSignatureError: abort(400)
        return 'OK'

    @handler.add(MessageEvent, message=TextMessage)
    def handle_line_message(event):
        resp = process_command(event.message.text, event.source.user_id, event.source.user_id, "line")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=resp))

def run_flask(): app_flask.run(host='0.0.0.0', port=5000, use_reloader=False)

if __name__ == '__main__':
    if LINE_CHANNEL_ACCESS_TOKEN:
        t = threading.Thread(target=run_flask); t.daemon = True; t.start()
    if TELEGRAM_TOKEN:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        for cmd in ['start', 'help', 'add', 'delete', 'update', 'today', 'week', 'spend', 'report', 'stock', 'weather', 's', 'remind']:
            app.add_handler(CommandHandler(cmd, t_cmd_wrapper))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), tg_msg_handler))
        app.run_polling()
