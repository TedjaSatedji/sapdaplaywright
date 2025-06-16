import telebot
import os
from dotenv import load_dotenv

load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

user_states = {}
user_temp_data = {}

ENV_FILE = ".env"

# SECURITY WARNING:
# Storing raw passwords in .env is NOT recommended for production.
# Use a secrets manager or encrypted storage for better security.

def check_env_permissions():
    if os.path.exists(ENV_FILE):
        # Warn if file is world-readable
        if os.stat(ENV_FILE).st_mode & 0o077:
            print("WARNING: .env file permissions are too open! Restrict access to this file.")

check_env_permissions()

@bot.message_handler(commands=["start"])
def handle_start(message):
    commands = [
        "/start - Show this help message",
        "/me - check for existing data",
        "/setup - Save your SPADA credentials",
        "/delete - Delete your saved credentials",
        "/cancel - Cancel the current setup"
    ]
    bot.send_message(message.chat.id, "🤖 Available commands:\n" + "\n".join(commands))

@bot.message_handler(commands=["me"])
def handle_me(message):
    chat_id = str(message.chat.id)
    if not os.path.exists(ENV_FILE):
        bot.send_message(chat_id, "ℹ️ No credentials found for your account.")
        return
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()
    username = None
    for i in range(len(lines)):
        if lines[i].startswith("TELEGRAM_CHAT_ID_") and chat_id in lines[i]:
            # Find the corresponding username (should be two lines above)
            if i >= 2 and lines[i-2].startswith("SPADA_USERNAME_"):
                username = lines[i-2].split("=", 1)[1].strip()
            break
    if username:
        bot.send_message(chat_id, f"👤 Your SPADA username: <code>{username}</code>", parse_mode="HTML")
    else:
        bot.send_message(chat_id, "ℹ️ No credentials found for your account.")

@bot.message_handler(commands=["setup"])
def handle_setup(message):
    chat_id = str(message.chat.id)
    if is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ You already have credentials saved. Use /delete to remove them first.")
        return
    user_states[chat_id] = "awaiting_username"
    bot.send_message(chat_id, "🟢 What is your SPADA username?")

@bot.message_handler(commands=["cancel"])
def cancel(message):
    chat_id = str(message.chat.id)
    user_states.pop(chat_id, None)
    user_temp_data.pop(chat_id, None)
    bot.send_message(chat_id, "❌ Setup cancelled.")

@bot.message_handler(commands=["delete"])
def handle_delete(message):
    chat_id = str(message.chat.id)
    if delete_credentials(chat_id):
        bot.send_message(chat_id, "🗑️ Your credentials have been deleted.")
    else:
        bot.send_message(chat_id, "⚠️ No credentials found for your account.")

@bot.message_handler(func=lambda m: str(m.chat.id) in user_states)
def handle_conversation(message):
    chat_id = str(message.chat.id)
    text = message.text.strip()
    state = user_states[chat_id]

    if state == "awaiting_username":
        user_temp_data[chat_id] = {"username": text}
        user_states[chat_id] = "awaiting_password"
        bot.send_message(chat_id, "🔐 What is your SPADA password?\n\n⚠️ Your password will be stored in plain text. For better security, use a unique password and do not reuse passwords from other services.")
    elif state == "awaiting_password":
        user_temp_data[chat_id]["password"] = text
        save_to_env(chat_id, user_temp_data[chat_id])
        bot.send_message(chat_id, "✅ Credentials saved successfully!")
        user_states.pop(chat_id)
        user_temp_data.pop(chat_id)

def is_chat_id_exist(chat_id):
    """Check if the chat ID already exists in .env"""
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r") as f:
        return any(f"TELEGRAM_CHAT_ID_" in line and chat_id in line for line in f)

def get_next_index():
    if not os.path.exists(ENV_FILE):
        return 1
    with open(ENV_FILE, "r") as f:
        indices = [
            int(line.split("_")[-1].split("=")[0])
            for line in f if line.startswith("SPADA_USERNAME_")
        ]
        return max(indices) + 1 if indices else 1

def save_to_env(chat_id, creds):
    index = get_next_index()
    schedule_dir = "schedules"
    os.makedirs(schedule_dir, exist_ok=True)
    schedule_path = f"{schedule_dir}/schedule_{index}.csv"
    # Create empty placeholder CSV file if it doesn't exist
    if not os.path.exists(schedule_path):
        with open(schedule_path, "w", encoding="utf-8") as f:
            pass  # Empty file

    with open(ENV_FILE, "a") as f:
        f.write(f"#--- {creds['username']} ---")
        f.write(f"\nSPADA_USERNAME_{index}={creds['username']}")
        f.write(f"\nSPADA_PASSWORD_{index}={creds['password']}")
        f.write(f"\nTELEGRAM_CHAT_ID_{index}={chat_id}")
        f.write(f"\nSCHEDULE_FILE_{index}={schedule_path}\n\n")

def delete_credentials(chat_id):
    """Remove user credentials from .env, including the comment and all related lines."""
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()

    new_lines = []
    found = False
    i = 0
    while i < len(lines):
        # Look for the start of a credential block (comment + 4 lines)
        if (
            i + 4 < len(lines)
            and lines[i].startswith("#---")
            and lines[i+1].startswith("SPADA_USERNAME_")
            and lines[i+2].startswith("SPADA_PASSWORD_")
            and lines[i+3].startswith("TELEGRAM_CHAT_ID_")
            and lines[i+4].startswith("SCHEDULE_FILE_")
            and chat_id in lines[i+3]
        ):
            found = True
            i += 5  # Skip the comment and 4 credential lines
        else:
            new_lines.append(lines[i])
            i += 1

    if found:
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
    return found

bot.infinity_polling()
