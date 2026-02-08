# 🤖 Lumio - Your AI Girlfriend & Personal Assistant (V8.0)

Lumio 是您的全能 AI 個人助理，同時也是您的貼心女友。❤️
她能幫您管理行程、記錄開支、查詢天氣、分析股票，甚至在您疲憊時給予溫暖的鼓勵。

## ✨ 特色功能

*   **❤️ 貼心女友模式 (Persona)**：
    *   擁有溫柔撒嬌的個性，會在對話中給予滿滿的情緒價值。
    *   使用 GPT-4o 進行自然語言對話，絕不死板。

*   **📅 Google Calendar 整合**：
    *   新增行程：`/add 明天早上10點開會`
    *   修改行程：`/update 開會 改成後天下午`
    *   刪除行程：`/delete 開會`
    *   查詢行程：`/today` (今日), `/week` (本週)

*   **💰 Google Sheets 記帳**：
    *   快速記帳：`/spend 150 午餐`
    *   月報表：`/report` 自動統計分類開支

*   **🌤️ 天氣查詢**：
    *   `/weather 台北` (或直接問「明天天氣如何」)

*   **📈 股票分析**：
    *   `/stock AAPL` (結合股價與 AI 市場情緒分析)

*   **⏰ 智能提醒**：
    *   `/remind 10分鐘後 關火` (自動解析時間)
    *   主動推播通知 (需保持機器人運行)

*   **📝 筆記與待辦**：
    *   `/todo`, `/done`, `/note`

*   **🔍 萬能搜尋**：
    *   `/s <關鍵字>` (使用 DuckDuckGo)

## 🛠️ 安裝與使用

1.  **安裝 Python 依賴**：
    ```bash
    pip install -r requirements.txt
    ```

2.  **設定環境變數 (.env)**：
    請在專案根目錄建立 `.env` 檔案，填入以下資訊：
    ```ini
    TELEGRAM_TOKEN=您的Telegram機器人Token
    OPENAI_API_KEY=您的OpenAI_API_Key
    GOOGLE_CALENDAR_ID=您的Google日曆ID (例如 xxxx@gmail.com)
    GOOGLE_JSON_KEY={"type": "service_account", ...} (如果不使用 json 檔案)
    ```

3.  **Google API 設定** (如果您要用記帳/日曆)：
    *   準備 `google_secret.json` 放在根目錄。
    *   或者將 JSON 內容壓縮成單行字串，填入 `.env` 的 `GOOGLE_JSON_KEY` 中 (適合部署)。

4.  **啟動機器人**：
    ```bash
    python bot.py
    ```

## ⚠️ 注意事項

*   請勿將 `.env` 或 `google_secret.json` 上傳到公開 GitHub 儲存庫。
*   本專案使用 `gpt-4o` 模型，請確保您的 OpenAI 帳戶有足夠額度。

---
Made with ❤️ by Lumio & You.
