import os
import sqlite3
import random
import logging
import threading
import asyncio
from flask import Flask, render_template_string, request, redirect, url_for, flash
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Logging Setup
logging.basicConfig(level=logging.INFO)

TOKEN = "8489532659:AAFlfn-oEgBiMWxSjGB3g7JFYgip_U9OBaU"
ADMIN_ID = 8843832406  
SUPPORT_USERNAME = "Aaryansh_0"
SUPPORT_LINK = f"https://t.me/{SUPPORT_USERNAME}"

DEFAULT_TASK_PRICE = 20.0  
MIN_WITHDRAWAL = 20.0      

# Professional Default Template
DEFAULT_TASK_CAPTION = (
    "⚡ **NEW TASK AVAILABLE!** ⚡\n\n"
    "🚀 Scan the QR quickly, complete the payment, and click **Done** below to grab your reward!\n\n"
    "🔥 *First come, first served! Be fast!*"
)

current_task = {
    "is_active": False,
    "photo_path": None,
    "caption": None,
    "price": DEFAULT_TASK_PRICE,
    "claimed_by_tg_id": None,
    "claimed_by_custom_id": None,
    "claimed_by_name": None,
    "user_msg_map": {}
}

bot_loop = None
tg_app = None

# Database Setup
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            custom_id INTEGER UNIQUE,
            first_name TEXT,
            username TEXT,
            qr_subscription INTEGER DEFAULT 0,
            balance REAL DEFAULT 0.0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            custom_id INTEGER,
            message_text TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value REAL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            custom_id INTEGER,
            first_name TEXT,
            photo_path TEXT,
            price REAL,
            status TEXT DEFAULT 'pending'
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('task_price', ?)", (DEFAULT_TASK_PRICE,))
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('min_withdrawal', ?)", (MIN_WITHDRAWAL,))
    conn.commit()
    conn.close()

init_db()

def get_setting(key, default):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else default

def set_setting(key, value):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, float(value)))
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect("bot_database.db")
    conn.row_factory = sqlite3.Row
    return conn

def generate_unique_5_digit_id():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    while True:
        random_id = random.randint(10000, 99999)
        cursor.execute("SELECT custom_id FROM users WHERE custom_id = ?", (random_id,))
        if not cursor.fetchone():
            conn.close()
            return random_id

def get_or_create_user(tg_id, first_name, username):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT custom_id, qr_subscription, balance FROM users WHERE telegram_id = ?", (tg_id,))
    user = cursor.fetchone()
    if not user:
        custom_id = generate_unique_5_digit_id()
        username_val = username if username else "Not Set"
        cursor.execute(
            "INSERT INTO users (telegram_id, custom_id, first_name, username, qr_subscription, balance) VALUES (?, ?, ?, ?, 0, 0.0)",
            (tg_id, custom_id, first_name, username_val)
        )
        conn.commit()
        sub_status, balance = 0, 0.0
    else:
        custom_id, sub_status, balance = user[0], user[1], user[2]
    conn.close()
    return custom_id, sub_status, balance

def add_user_balance(tg_id, amount):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, tg_id))
    cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,))
    new_bal = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return new_bal

def deduct_user_balance(tg_id, amount):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?", (amount, tg_id))
    cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,))
    new_bal = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return new_bal

def toggle_user_subscription(tg_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT qr_subscription FROM users WHERE telegram_id = ?", (tg_id,))
    res = cursor.fetchone()
    new_status = 1 if res and res[0] == 0 else 0
    cursor.execute("UPDATE users SET qr_subscription = ? WHERE telegram_id = ?", (new_status, tg_id))
    conn.commit()
    conn.close()
    return new_status

def log_user_activity(custom_id, text):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (custom_id, message_text) VALUES (?, ?)", (custom_id, text))
    conn.commit()
    conn.close()

def get_subscribed_users():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, custom_id FROM users WHERE qr_subscription = 1")
    users = cursor.fetchall()
    conn.close()
    return users

def get_main_keyboard(sub_status):
    status_btn_text = "🟢 Get QR Status: ENABLED" if sub_status == 1 else "🔴 Get QR Status: DISABLED"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(status_btn_text, callback_data="toggle_qr")],
        [InlineKeyboardButton("💳 Request Withdrawal", callback_data="start_withdrawal")],
        [InlineKeyboardButton("💬 Contact Support", url=SUPPORT_LINK)]
    ])

# Broadcast Active Task
async def broadcast_active_task():
    global current_task, tg_app
    sub_users = get_subscribed_users()
    if not sub_users or not current_task["photo_path"]:
        return 0

    current_task["is_active"] = True
    current_task["claimed_by_tg_id"] = None
    current_task["claimed_by_custom_id"] = None
    current_task["user_msg_map"] = {}

    claim_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ CLAIM TASK FAST (₹20)", callback_data="claim_task")]])

    sent_count = 0
    with open(current_task["photo_path"], 'rb') as photo_file:
        photo_bytes = photo_file.read()

    for tg_id, c_id in sub_users:
        try:
            sent_msg = await tg_app.bot.send_photo(
                chat_id=tg_id,
                photo=photo_bytes,
                caption=(
                    f"{current_task['caption']}\n\n"
                    f"💰 **Reward Price:** ₹{current_task['price']:.2f}\n\n"
                    f"👇 Click the button below instantly to claim!"
                ),
                parse_mode="Markdown",
                reply_markup=claim_keyboard
            )
            current_task["user_msg_map"][tg_id] = sent_msg.message_id
            sent_count += 1
        except Exception:
            pass
    return sent_count

# Helper to send async message from Flask
async def send_user_tg_notification(tg_id, text):
    global tg_app
    try:
        await tg_app.bot.send_message(chat_id=tg_id, text=text, parse_mode="Markdown")
    except Exception:
        pass

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data["withdraw_state"] = None
    custom_id, sub_status, balance = get_or_create_user(user.id, user.first_name, user.username)
    min_wd = get_setting('min_withdrawal', MIN_WITHDRAWAL)
    
    msg = (
        f"👋 Hello {user.first_name}!\n\n"
        f"🆔 Your Account ID: `{custom_id}`\n"
        f"💰 Wallet Balance: ₹{balance:.2f}\n"
        f"📌 Minimum Withdrawal: ₹{min_wd:.2f}\n\n"
        f"💬 Need help? [Contact Support]({SUPPORT_LINK})"
    )
    await update.message.reply_text(
        msg, 
        parse_mode="Markdown", 
        reply_markup=get_main_keyboard(sub_status),
        disable_web_page_preview=True
    )

async def track_user_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    msg_text = update.message.text.strip()
    custom_id, sub_status, balance = get_or_create_user(user.id, user.first_name, user.username)
    min_wd = get_setting('min_withdrawal', MIN_WITHDRAWAL)

    # WITHDRAWAL FLOW
    if context.user_data.get("withdraw_state") == "awaiting_upi":
        context.user_data["withdraw_upi"] = msg_text
        context.user_data["withdraw_state"] = "awaiting_amount"
        await update.message.reply_text(
            f"✅ UPI ID Saved: `{msg_text}`\n\n"
            f"💰 Your Balance: ₹{balance:.2f}\n"
            f"📌 Enter Amount to withdraw (Minimum ₹{min_wd:.2f}):",
            parse_mode="Markdown"
        )
        return

    if context.user_data.get("withdraw_state") == "awaiting_amount":
        try:
            amount = float(msg_text)
        except ValueError:
            await update.message.reply_text("⚠️ Enter a valid number.")
            return

        if amount < min_wd:
            await update.message.reply_text(f"❌ Minimum withdrawal is ₹{min_wd:.2f}.")
            return

        if amount > balance:
            await update.message.reply_text(f"❌ Insufficient Balance! Your balance is ₹{balance:.2f}.")
            return

        upi_id = context.user_data.get("withdraw_upi")
        context.user_data["withdraw_state"] = None

        await update.message.reply_text(
            f"⏳ **Withdrawal Request Submitted!**\n\n"
            f"💵 Amount: ₹{amount:.2f}\n"
            f"🏦 UPI ID: `{upi_id}`\n\n"
            f"Status: Pending Admin Approval.",
            parse_mode="Markdown"
        )

        admin_approval_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve Payment", callback_data=f"admin_approve_wd_{user.id}_{custom_id}_{amount}"),
             InlineKeyboardButton("❌ Reject Request", callback_data=f"admin_reject_wd_{user.id}_{custom_id}_{amount}")]
        ])

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🚨 **NEW WITHDRAWAL REQUEST!**\n\n"
                    f"👤 **User:** {user.first_name} (@{user.username or 'N/A'})\n"
                    f"🆔 **User ID:** `{custom_id}`\n"
                    f"💵 **Amount:** ₹{amount:.2f}\n"
                    f"🏦 **UPI ID:** `{upi_id}`"
                ),
                parse_mode="Markdown",
                reply_markup=admin_approval_keyboard
            )
        except Exception:
            pass
        return

    log_user_activity(custom_id, msg_text)

    response_text = (
        f"👋 Hello {user.first_name}!\n\n"
        f"🆔 Account ID: `{custom_id}`\n"
        f"💰 Balance: ₹{balance:.2f}\n\n"
        f"💬 For support: [Contact Support]({SUPPORT_LINK})"
    )
    await update.message.reply_text(
        response_text, 
        parse_mode="Markdown", 
        reply_markup=get_main_keyboard(sub_status),
        disable_web_page_preview=True
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    custom_id, sub_status, balance = get_or_create_user(user.id, user.first_name, user.username)
    min_wd = get_setting('min_withdrawal', MIN_WITHDRAWAL)
    global current_task

    if query.data == "toggle_qr":
        new_status = toggle_user_subscription(user.id)
        alert_msg = "✅ Get QR enabled!" if new_status == 1 else "❌ Get QR disabled!"
        await query.answer(alert_msg, show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=get_main_keyboard(new_status))
        except Exception:
            pass

    elif query.data == "start_withdrawal":
        if balance < min_wd:
            await query.answer(f"❌ Minimum ₹{min_wd:.2f} required to withdraw!", show_alert=True)
            return

        context.user_data["withdraw_state"] = "awaiting_upi"
        await query.answer()
        await query.message.reply_text(
            f"💳 **Withdrawal Request**\n\n"
            f"💰 Available Balance: ₹{balance:.2f}\n\n"
            f"👉 Reply with your **UPI ID**:",
            parse_mode="Markdown"
        )

    # CLAIM TASK
    elif query.data == "claim_task":
        if not current_task["is_active"]:
            await query.answer("⚠️ Another user already claimed this QR task!", show_alert=True)
            return

        current_task["is_active"] = False
        current_task["claimed_by_tg_id"] = user.id
        current_task["claimed_by_custom_id"] = custom_id
        current_task["claimed_by_name"] = user.first_name

        await query.answer("🎉 Task Claimed Successfully! Complete & Click Done.", show_alert=True)

        action_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done (Payment Completed)", callback_data="user_task_done"),
             InlineKeyboardButton("⏭️ Pass Task", callback_data="user_task_pass")]
        ])

        try:
            await query.edit_message_caption(
                caption="⚠️ **You have claimed this task!**\nComplete the transaction via QR and click **Done**, or click **Pass** to release it.",
                parse_mode="Markdown",
                reply_markup=action_keyboard
            )
        except Exception:
            pass

        expired_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CLAIMED BY ANOTHER USER", callback_data="claimed_done")]])
        for tg_id, msg_id in current_task["user_msg_map"].items():
            if tg_id != user.id:
                try:
                    await context.bot.edit_message_caption(
                        chat_id=tg_id,
                        message_id=msg_id,
                        caption="⚠️ **TASK EXPIRED!**\nAnother user claimed this QR task faster.",
                        parse_mode="Markdown",
                        reply_markup=expired_markup
                    )
                except Exception:
                    pass

    # USER TASK PASS
    elif query.data == "user_task_pass":
        if current_task["claimed_by_tg_id"] != user.id:
            await query.answer("This is not your active task.", show_alert=True)
            return

        await query.answer("Task Passed! Broadcasting back to others...", show_alert=True)
        try:
            await query.edit_message_caption(caption="⏭️ You passed this QR Task.")
        except Exception:
            pass

        await broadcast_active_task()

    # USER TASK DONE (Save to Database for Web Admin Approval)
    elif query.data == "user_task_done":
        if current_task["claimed_by_tg_id"] != user.id:
            await query.answer("This is not your active task.", show_alert=True)
            return

        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_tasks (telegram_id, custom_id, first_name, photo_path, price) VALUES (?, ?, ?, ?, ?)",
            (user.id, custom_id, user.first_name, current_task["photo_path"], current_task["price"])
        )
        conn.commit()
        conn.close()

        await query.answer("Submitted to Admin Web Panel for verification!", show_alert=True)
        try:
            await query.edit_message_caption(caption="⏳ **Submitted for Verification!**\nAdmin will verify your payment and add balance shortly.")
        except Exception:
            pass

    # ADMIN WITHDRAWAL APPROVALS (From Telegram)
    elif query.data.startswith("admin_approve_wd_"):
        parts = query.data.split("_")
        u_tg_id = int(parts[3])
        u_custom_id = parts[4]
        wd_amount = float(parts[5])

        _, _, latest_bal = get_or_create_user(u_tg_id, "", "")
        if latest_bal < wd_amount:
            await query.answer("Insufficient balance!", show_alert=True)
            return

        new_bal = deduct_user_balance(u_tg_id, wd_amount)
        await query.answer("Approved!", show_alert=False)
        try:
            await query.edit_message_text(
                text=f"✅ **WITHDRAWAL APPROVED!**\nProcessed ₹{wd_amount:.2f} for User `{u_custom_id}`.\nRemaining: ₹{new_bal:.2f}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=u_tg_id,
                text=f"🎉 **Withdrawal Approved!**\n₹{wd_amount:.2f} transferred.\n💳 Remaining Balance: ₹{new_bal:.2f}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    elif query.data.startswith("admin_reject_wd_"):
        parts = query.data.split("_")
        u_tg_id = int(parts[3])
        u_custom_id = parts[4]
        wd_amount = float(parts[5])

        await query.answer("Rejected!", show_alert=False)
        try:
            await query.edit_message_text(
                text=f"❌ **WITHDRAWAL REJECTED!**\nRequest of ₹{wd_amount:.2f} for User `{u_custom_id}` rejected.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=u_tg_id,
                text=f"❌ **Withdrawal Rejected!**\nYour request for ₹{wd_amount:.2f} was declined.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    elif query.data == "claimed_done":
        await query.answer("Task finalized.", show_alert=False)

# --- FLASK DASHBOARD ---
app = Flask(__name__)
app.secret_key = "super_secret_admin_key"

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Admin Control Panel</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; background: #eef2f5; margin: 0; padding: 15px; color: #333; }
        .container { max-width: 950px; margin: auto; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
        h2, h3 { color: #007bff; margin-top: 0; }
        label { font-weight: bold; display: block; margin-top: 10px; }
        input, textarea, select, button { width: 100%; padding: 10px; margin-top: 5px; border-radius: 5px; border: 1px solid #ccc; box-sizing: border-box; }
        button { background: #28a745; color: white; font-size: 15px; font-weight: bold; border: none; cursor: pointer; margin-top: 5px; }
        .btn-blue { background: #007bff; }
        .btn-red { background: #dc3545; }
        .alert { background: #d4edda; color: #155724; padding: 10px; border-radius: 5px; margin-bottom: 15px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; background: white; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; vertical-align: middle; }
        th { background-color: #007bff; color: white; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .pending-card { border-left: 5px solid #ffc107; background: #fffdf5; }
        @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h2>🛠️ Master Control Panel</h2>
            <p>Manage Tasks, Web Approvals & User Wallets</p>
        </div>

        {% with messages = get_flashed_messages() %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        <!-- PENDING APPROVALS SECTION -->
        <div class="card pending-card">
            <h3>⏳ Pending Task Approvals (Done Requests)</h3>
            {% if pending_tasks %}
                <table>
                    <tr>
                        <th>User ID</th>
                        <th>Name</th>
                        <th>Task Price</th>
                        <th>Action</th>
                    </tr>
                    {% for task in pending_tasks %}
                    <tr>
                        <td><b>{{ task['custom_id'] }}</b></td>
                        <td>{{ task['first_name'] }}</td>
                        <td>₹{{ "%.2f"|format(task['price']) }}</td>
                        <td>
                            <form action="/approve_task" method="POST" style="display:inline-block; width:48%;">
                                <input type="hidden" name="task_id" value="{{ task['id'] }}">
                                <button type="submit" class="btn-blue">✅ Accept (+₹{{ "%.0f"|format(task['price']) }})</button>
                            </form>
                            <form action="/reject_task" method="POST" style="display:inline-block; width:48%;">
                                <input type="hidden" name="task_id" value="{{ task['id'] }}">
                                <button type="submit" class="btn-red">❌ Reject</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            {% else %}
                <p style="color: #666; margin-bottom: 0;">No pending task approvals right now.</p>
            {% endif %}
        </div>

        <!-- BROADCAST TASK -->
        <div class="card">
            <h3>📤 Send QR Task (Broadcast)</h3>
            <form action="/broadcast_task" method="POST" enctype="multipart/form-data">
                <label>Select QR Photo:</label>
                <input type="file" name="qr_photo" accept="image/*" required>
                
                <label>Task Description / Caption (Pre-filled Professional Template):</label>
                <textarea name="caption" rows="4" required>{{ default_caption }}</textarea>
                
                <label>Reward Amount (₹):</label>
                <input type="number" step="0.01" name="price" value="{{ task_price }}" required>
                
                <button type="submit" class="btn-blue">⚡ Send Task To Users (Fast Claim)</button>
            </form>
        </div>

        <!-- SETTINGS -->
        <div class="card">
            <h3>⚙️ System Settings</h3>
            <form action="/update_settings" method="POST">
                <div class="grid">
                    <div>
                        <label>Default Task Price (₹):</label>
                        <input type="number" step="0.01" name="task_price" value="{{ task_price }}" required>
                    </div>
                    <div>
                        <label>Min Withdrawal (₹):</label>
                        <input type="number" step="0.01" name="min_withdrawal" value="{{ min_withdrawal }}" required>
                    </div>
                </div>
                <button type="submit">Save Settings</button>
            </form>
        </div>

        <!-- BALANCE CONTROL -->
        <div class="card">
            <h3>💰 Manage User Balance</h3>
            <form action="/update_user_balance" method="POST">
                <div class="grid">
                    <div>
                        <label>User Custom ID:</label>
                        <input type="number" name="custom_id" placeholder="e.g. 48201" required>
                    </div>
                    <div>
                        <label>Action:</label>
                        <select name="action">
                            <option value="add">Add (+)</option>
                            <option value="deduct">Deduct (-)</option>
                            <option value="set">Set (=)</option>
                        </select>
                    </div>
                </div>
                <label>Amount (₹):</label>
                <input type="number" step="0.01" name="amount" required>
                <button type="submit">Update Wallet</button>
            </form>
        </div>

        <!-- USER LIST -->
        <div class="card">
            <h3>👥 Registered Users</h3>
            <table>
                <tr>
                    <th>Custom ID</th>
                    <th>Name</th>
                    <th>Username</th>
                    <th>Balance</th>
                    <th>Get QR</th>
                </tr>
                {% for u in users %}
                <tr>
                    <td><b>{{ u['custom_id'] }}</b></td>
                    <td>{{ u['first_name'] }}</td>
                    <td>@{{ u['username'] }}</td>
                    <td>₹{{ "%.2f"|format(u['balance']) }}</td>
                    <td>{% if u['qr_subscription'] == 1 %}<span style="color:green;font-weight:bold;">🟢 ON</span>{% else %}<span style="color:red;">🔴 OFF</span>{% endif %}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
    </div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users ORDER BY custom_id DESC").fetchall()
    pending_tasks = conn.execute("SELECT * FROM pending_tasks WHERE status = 'pending' ORDER BY id DESC").fetchall()
    conn.close()
    
    t_price = get_setting('task_price', DEFAULT_TASK_PRICE)
    m_wd = get_setting('min_withdrawal', MIN_WITHDRAWAL)
    
    return render_template_string(
        HTML_TEMPLATE, 
        users=users, 
        pending_tasks=pending_tasks,
        task_price=t_price, 
        min_withdrawal=m_wd,
        default_caption=DEFAULT_TASK_CAPTION
    )

@app.route("/approve_task", methods=["POST"])
def approve_task():
    global bot_loop
    task_id = request.form.get("task_id")

    conn = get_db_connection()
    task = conn.execute("SELECT * FROM pending_tasks WHERE id = ?", (task_id,)).fetchone()

    if task and task['status'] == 'pending':
        tg_id = task['telegram_id']
        price = task['price']
        custom_id = task['custom_id']

        conn.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (price, tg_id))
        conn.execute("UPDATE pending_tasks SET status = 'approved' WHERE id = ?", (task_id,))
        conn.commit()

        new_bal = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()[0]
        conn.close()

        msg = f"🎉 **QR Approved!**\n₹{price:.2f} added to your account!\n💳 New Balance: ₹{new_bal:.2f}"
        asyncio.run_coroutine_threadsafe(send_user_tg_notification(tg_id, msg), bot_loop)

        flash(f"✅ Approved Task for User {custom_id}! Added ₹{price:.2f}.")
    else:
        conn.close()

    return redirect(url_for("dashboard"))

@app.route("/reject_task", methods=["POST"])
def reject_task():
    global bot_loop
    task_id = request.form.get("task_id")

    conn = get_db_connection()
    task = conn.execute("SELECT * FROM pending_tasks WHERE id = ?", (task_id,)).fetchone()

    if task and task['status'] == 'pending':
        tg_id = task['telegram_id']
        custom_id = task['custom_id']

        conn.execute("UPDATE pending_tasks SET status = 'rejected' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()

        msg = "❌ **QR FAILED!**\nDon't fail QRs. Please check your UPI ID or details."
        asyncio.run_coroutine_threadsafe(send_user_tg_notification(tg_id, msg), bot_loop)

        flash(f"❌ Rejected Task for User {custom_id}.")
    else:
        conn.close()

    return redirect(url_for("dashboard"))

@app.route("/broadcast_task", methods=["POST"])
def broadcast_task():
    global current_task, bot_loop
    file = request.files.get("qr_photo")
    caption = request.form.get("caption", DEFAULT_TASK_CAPTION)
    price = float(request.form.get("price", DEFAULT_TASK_PRICE))

    if file:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)

        current_task["photo_path"] = filepath
        current_task["caption"] = caption
        current_task["price"] = price

        future = asyncio.run_coroutine_threadsafe(broadcast_active_task(), bot_loop)
        sent_count = future.result()

        flash(f"✅ Fast Task Broadcasted successfully to {sent_count} active users!")
    return redirect(url_for("dashboard"))

@app.route("/update_settings", methods=["POST"])
def update_settings():
    task_price = request.form.key = request.form.get("task_price")
    min_withdrawal = request.form.get("min_withdrawal")

    if task_price:
        set_setting('task_price', task_price)
    if min_withdrawal:
        set_setting('min_withdrawal', min_withdrawal)

    flash("✅ System Settings Updated!")
    return redirect(url_for("dashboard"))

@app.route("/update_user_balance", methods=["POST"])
def update_user_balance():
    custom_id = request.form.get("custom_id")
    action = request.form.get("action")
    amount = float(request.form.get("amount", 0))

    conn = get_db_connection()
    if action == "add":
        conn.execute("UPDATE users SET balance = balance + ? WHERE custom_id = ?", (amount, custom_id))
    elif action == "deduct":
        conn.execute("UPDATE users SET balance = balance - ? WHERE custom_id = ?", (amount, custom_id))
    elif action == "set":
        conn.execute("UPDATE users SET balance = ? WHERE custom_id = ?", (amount, custom_id))
    
    conn.commit()
    conn.close()

    flash(f"✅ Balance updated for User ID {custom_id}!")
    return redirect(url_for("dashboard"))

def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

def main():
    global bot_loop, tg_app
    bot_loop = asyncio.get_event_loop()

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    tg_app = ApplicationBuilder().token(TOKEN).build()
    
    tg_app.add_handler(CommandHandler("start", start_command))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_user_activity))

    print("==================================================")
    print("✅ Professional Web Control Panel & Bot Running!")
    print("🌐 Dashboard URL: http://127.0.0.1:5000")
    print("==================================================")

    tg_app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
