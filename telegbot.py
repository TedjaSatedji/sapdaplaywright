import telebot
import os
import io
import base64
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

user_states = {}
user_temp_data = {}
pending_csv = {}  # cache extracted CSV waiting for confirmation

ENV_FILE = ".env"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Gemini Setup ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

# --- Security check for .env ---
def check_env_permissions():
    if os.path.exists(ENV_FILE):
        if os.stat(ENV_FILE).st_mode & 0o077:
            print("WARNING: .env file permissions are too open! Restrict access to this file.")

check_env_permissions()

# --- Helpers ---
def is_chat_id_exist(chat_id):
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
    if not os.path.exists(schedule_path):
        with open(schedule_path, "w", encoding="utf-8") as f:
            pass
    with open(ENV_FILE, "a") as f:
        f.write(f"#--- {creds['username']} ---")
        f.write(f"\nSPADA_USERNAME_{index}={creds['username']}")
        f.write(f"\nSPADA_PASSWORD_{index}={creds['password']}")
        f.write(f"\nTELEGRAM_CHAT_ID_{index}={chat_id}")
        f.write(f"\nSCHEDULE_FILE_{index}={schedule_path}\n")

def delete_credentials(chat_id):
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()
    new_lines = []
    found = False
    i = 0
    while i < len(lines):
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
            i += 5
        else:
            new_lines.append(lines[i])
            i += 1
    if found:
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
    return found

# --- Gemini helper ---
def parse_schedule_with_gemini(image_bytes):
    """Send an image to Gemini API using google.generativeai and get structured CSV content back"""
    image_data = {
        "mime_type": "image/png",
        "data": image_bytes
    }

    prompt = (
        "Extract the class schedule from this image and return only CSV rows. "
        "Columns must be in this exact order: CourseName,Day,Time. "
        "Example:\n"
        "CourseName,Day,Time"
        "Matematika,Senin,07:00 - 09:00\n"
        "Fisika,Rabu,10:00 - 12:00\n"
        "Always add column name"
        "Do not forget the space before and after hyphen for the time"
        "Do not include, explanations, or extra text."
    )

    response = gemini_model.generate_content([prompt, image_data])
    return response.text.strip()

# --- Commands ---
@bot.message_handler(commands=["start"])
def handle_start(message):
    commands = [
        "/start - Show this help message",
        "/me - Check your saved SPADA username",
        "/setup - Save your SPADA credentials",
        "/delete - Delete your credentials",
        "/cancel - Cancel setup",
        "📷 Send a photo of your schedule to auto-generate CSV"
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
        bot.send_message(chat_id, "⚠️ You already have credentials saved. Use /delete first.")
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
        bot.send_message(chat_id, "🔐 What is your SPADA password?\n\n⚠️ Stored in plain text. Use a unique password.")
    elif state == "awaiting_password":
        user_temp_data[chat_id]["password"] = text
        save_to_env(chat_id, user_temp_data[chat_id])
        bot.send_message(chat_id, "✅ Credentials saved successfully!")
        user_states.pop(chat_id)
        user_temp_data.pop(chat_id)

# --- Photo handler ---
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = str(message.chat.id)

    if not is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ Please run /setup first before sending your schedule.")
        return

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded = bot.download_file(file_info.file_path)

    bot.send_message(chat_id, "⏳ Processing your schedule image with Gemini...")

    try:
        csv_text = parse_schedule_with_gemini(downloaded)
        pending_csv[chat_id] = csv_text

        csv_file = io.BytesIO(csv_text.encode("utf-8"))
        csv_file.name = "schedule_preview.csv"
        bot.send_document(chat_id, csv_file, caption="📄 Here’s the schedule I extracted.\nReply 'yes' to save, or 'no' to cancel.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error parsing schedule: {e}")

# --- Confirmation handler ---
@bot.message_handler(func=lambda m: str(m.chat.id) in pending_csv and m.text.lower() in ["yes", "no"])
def handle_confirmation(message):
    chat_id = str(message.chat.id)
    response = message.text.lower()

    if response == "yes":
        schedule_path = None
        user_index = None

        # Find index for this chat_id
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith("TELEGRAM_CHAT_ID_") and chat_id in line:
                user_index = line.split("_")[-1].split("=")[0]  # e.g. "12"
                break

        if user_index:
            for line in lines:
                if line.startswith(f"SCHEDULE_FILE_{user_index}="):
                    schedule_path = line.strip().split("=", 1)[1]
                    break

        if not schedule_path:
            bot.send_message(chat_id, "❌ Could not locate your schedule file in .env.")
            pending_csv.pop(chat_id, None)
            return

        # Save CSV to that path
        with open(schedule_path, "w", encoding="utf-8") as f:
            f.write(pending_csv[chat_id])

        bot.send_message(chat_id, f"✅ Schedule saved to {schedule_path}")
    else:
        bot.send_message(chat_id, "❌ Schedule upload cancelled. Please resend if needed.")

    pending_csv.pop(chat_id, None)


# --- Run bot ---
bot.infinity_polling()
