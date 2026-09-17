import os
import json
import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import anthropic
import caldav
import icalendar

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)

JST = ZoneInfo("Asia/Tokyo")

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

pending_clarifications = {}


def get_calendar_service():
    creds_info = json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(creds_info)
    return build("calendar", "v3", credentials=creds)


def add_google_calendar_event(title, start_dt, end_dt=None):
    service = get_calendar_service()
    if end_dt is None:
        end_dt = start_dt + datetime.timedelta(hours=1)
    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Tokyo"},
    }
    service.events().insert(calendarId="primary", body=event).execute()


def add_icloud_reminder(title, due_dt):
    apple_id = os.environ["ICLOUD_APPLE_ID"]
    app_password = os.environ["ICLOUD_APP_PASSWORD"]

    client = caldav.DAVClient(
        url="https://caldav.icloud.com/",
        username=apple_id,
        password=app_password,
    )
    principal = client.principal()
    calendars = principal.calendars()
    calendar = calendars[0]
    for c in calendars:
        try:
            if "VTODO" in c.get_supported_components():
                calendar = c
                break
        except Exception:
            pass

    cal = icalendar.Calendar()
    todo = icalendar.Todo()
    todo.add("summary", title)
    if due_dt:
        todo.add("due", due_dt)
    cal.add_component(todo)

    calendar.save_event(cal.to_ical().decode("utf-8"))


PARSE_SYSTEM_PROMPT = """あなたはLINEに投稿されたタスクを解析するアシスタントです。
ユーザーの投稿から「タスク内容」と「日時」を抽出してください。

現在日時: {now}

以下のJSON形式のみで回答してください。説明文やコードブロックは一切つけないこと。

{{
  "title": "タスクの内容(短く)",
  "datetime": "YYYY-MM-DDTHH:MM:SS 形式、日時が明確な場合のみ。不明ならnull",
  "needs_clarification": true または false,
  "clarification_question": "日時が曖昧な場合にユーザーに聞き返す質問文。明確な場合はnull"
}}

判定基準:
- 日時が具体的に特定できる場合は needs_clarification: false
- 日時の手がかりが全くない場合は needs_clarification: true
- 大まかでも1日以内に絞れる場合は妥当な時刻を補って明確扱いにしてよい
"""


def parse_task(user_text):
    now = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M (%A)")
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=PARSE_SYSTEM_PROMPT.format(now=now),
        messages=[{"role": "user", "content": user_text}],
    )
    text = resp.content[0].text.strip()
    return json.loads(text)


def parse_clarified_datetime(original_text, clarification_reply):
    now = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M (%A)")
    combined = "元のタスク: " + original_text + "\nユーザーの補足: " + clarification_reply
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=PARSE_SYSTEM_PROMPT.format(now=now),
        messages=[{"role": "user", "content": combined}],
    )
    text = resp.content[0].text.strip()
    return json.loads(text)


@app.route("/")
def index():
    return "AI秘書Bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


def reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def register_task(title, dt_str):
    start_dt = None
    if dt_str:
        start_dt = datetime.datetime.fromisoformat(dt_str).replace(tzinfo=JST)
        add_google_calendar_event(title, start_dt)
    add_icloud_reminder(title, start_dt)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if user_id in pending_clarifications:
        original_text = pending_clarifications.pop(user_id)["original_text"]
        result = parse_clarified_datetime(original_text, text)
    else:
        result = parse_task(text)

    if result.get("needs_clarification"):
        pending_clarifications[user_id] = {"original_text": text}
        reply(event.reply_token, result["clarification_question"])
        return

    title = result["title"]
    dt_str = result.get("datetime")

    try:
        register_task(title, dt_str)
    except Exception as e:
        reply(event.reply_token, "登録に失敗しました…もう一度試してもらえますか?\n(" + str(e) + ")")
        return

    if dt_str:
        dt_display = datetime.datetime.fromisoformat(dt_str).strftime("%m/%d %H:%M")
        reply(event.reply_token, "登録したよ📅\n「" + title + "」\n" + dt_display)
    else:
        reply(event.reply_token, "リマインダーに追加したよ✅\n「" + title + "」")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
