import os
import io
from dotenv import load_dotenv
import telebot
from telebot import types
import google.generativeai as genai
from datetime import datetime

FLAG_DIR = "flags"
os.makedirs(FLAG_DIR, exist_ok=True)

# =============================
# Boot
# =============================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENV_FILE = ".env"

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# =============================
# State
# =============================
user_states = {}        # setup convo states
user_temp_data = {}     # temp creds during setup
waiting_upload = set()  # chat_ids currently asked to upload an image
pending_csv = {}        # chat_id -> csv text awaiting Save/Cancel

# =============================
# Gemini
# =============================
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

def parse_schedule_with_gemini(image_bytes: bytes) -> str:
    """
    Use Gemini to parse the schedule image and return CSV rows (no header).
    Required column order: CourseName,Day,Time
    """
    image_data = {
        "mime_type": "image/jpeg",  # Telegram photos are JPEG; still works for PNG
        "data": image_bytes
    }
    prompt = (
        "Extract the class schedule from this image and return only CSV rows. "
        "Columns must be in this exact order: CourseName,Day,Time. "
        "Example:\n"
        "CourseName,Day,Time\n"
        "Matematika,Senin,07:00 - 09:00\n"
        "Fisika,Rabu,10:00 - 12:00\n"
        "Always add column name\n"
		"Do not add ```csv``` or any code fences\n"
        "Do not forget the space before and after hyphen for the time\n"
        "Do not include class, explanations, or extra text. only Course Name, Day and Time."
    )
    resp = gemini_model.generate_content([prompt, image_data])
    return (resp.text or "").strip()

# =============================
# Helpers
# =============================
def is_chat_id_exist(chat_id: str) -> bool:
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("TELEGRAM_CHAT_ID_") and chat_id in line:
                return True
    return False

def find_user_index_by_chat(chat_id: str):
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("TELEGRAM_CHAT_ID_") and chat_id in line:
                return line.split("_")[-1].split("=")[0].strip()
    return None

def find_schedule_path(chat_id: str) -> str | None:
    idx = find_user_index_by_chat(chat_id)
    if not idx:
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"SCHEDULE_FILE_{idx}="):
                return line.strip().split("=", 1)[1]
    return None

def get_next_index():
    if not os.path.exists(ENV_FILE):
        return 1
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        indices = [
            int(line.split("_")[-1].split("=")[0])
            for line in f if line.startswith("SPADA_USERNAME_")
        ]
        return max(indices) + 1 if indices else 1

def save_to_env(chat_id: str, creds: dict):
    """
    Writes new creds to .env (existing behavior for setup).
    """
    index = get_next_index()
    schedule_dir = "schedules"
    os.makedirs(schedule_dir, exist_ok=True)
    schedule_path = f"{schedule_dir}/schedule_{index}.csv"
    if not os.path.exists(schedule_path):
        open(schedule_path, "w", encoding="utf-8").close()
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"#--- {creds['username']} ---\n")
        f.write(f"SPADA_USERNAME_{index}={creds['username']}\n")
        f.write(f"SPADA_PASSWORD_{index}={creds['password']}\n")
        f.write(f"TELEGRAM_CHAT_ID_{index}={chat_id}\n")
        f.write(f"SCHEDULE_FILE_{index}={schedule_path}\n")

def delete_credentials(chat_id: str) -> bool:
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = []
    found = False
    i = 0
    schedule_path = None
    username = None
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
            username = lines[i+1].strip().split("=", 1)[1]
            schedule_path = lines[i+4].strip().split("=", 1)[1]
            i += 5  # skip this block
        else:
            new_lines.append(lines[i])
            i += 1
    if found:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        # Delete schedule file
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
            except Exception:
                pass
        # Delete all flag files for this user
        if username:
            for f in os.listdir(FLAG_DIR):
                if f.startswith(f"pause_user_{username}") or f.startswith(f"pause_once_{username}_"):
                    try:
                        os.remove(os.path.join(FLAG_DIR, f))
                    except Exception:
                        pass
    return found

def schedule_menu_markup():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🖼 Upload Schedule", callback_data="sch_upload"),
        types.InlineKeyboardButton("⬆️ Upload CSV", callback_data="sch_upload_csv"),
        types.InlineKeyboardButton("📄 View Schedule", callback_data="sch_view"),
    )
    kb.add(types.InlineKeyboardButton("🗑 Delete Schedule", callback_data="sch_delete"))
    return kb

def confirm_menu_markup():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Save", callback_data="sch_save"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel"),
    )
    return kb

def find_username_by_chat(chat_id: str) -> str | None:
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith("TELEGRAM_CHAT_ID_") and chat_id in line:
            if i >= 2 and lines[i-2].startswith("SPADA_USERNAME_"):
                return lines[i-2].split("=", 1)[1].strip()
    return None

def get_next_class(schedule_path: str):
    if not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
        return None
    now = datetime.now()
    closest_class = None
    closest_start = None
    with open(schedule_path, "r", encoding="utf-8") as f:
        lines = f.readlines()[1:]  # skip header
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        course, day, time_str = parts
        start_str, end_str = time_str.split(" - ")
        try:
            start_time = datetime.strptime(start_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
        except:
            continue
        if start_time > now:
            if closest_start is None or start_time < closest_start:
                closest_start = start_time
                closest_class = course
    return closest_class

# =============================
# Commands
# =============================
@bot.message_handler(commands=["help"])
def handle_help(message):
    bot.send_message(
        message.chat.id,
        "hi hi~ 💫\n\n"
        "Here’s what I can do for you:\n"
        "• <b>/help</b> – show this help message\n"
        "• <b>/setup</b> – link your SPADA account\n"
        "• <b>/mystatus</b> – show your SPADA user, schedule, and pause status\n"
        "• <b>/pause</b> – pause attendance indefinitely\n"
        "• <b>/resume</b> – resume attendance if paused\n"
        "• <b>/pauseonce</b> – skip attendance for your next class\n"
        "• <b>/delete</b> – remove your saved credentials\n"
        "• <b>/schedule</b> – manage your class schedule (upload/view/delete)\n"
        "• <b>/cancel</b> – cancel any ongoing action\n"
    )

@bot.message_handler(commands=["mystatus"])
def cmd_mystatus(message):
    chat_id = str(message.chat.id)
    username = find_username_by_chat(chat_id)
    schedule_path = find_schedule_path(chat_id)

    if not username:
        bot.send_message(chat_id, "⚠️ No linked SPADA user found.")
        return

    # Check pause states
    pause_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    pause_state = "▶️ Active"
    if os.path.exists(pause_file):
        pause_state = "⏸️ Paused indefinitely"
    else:
        # Check for any pause_once flags
        once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
        if once_flags:
            paused_class = once_flags[0].replace(f"pause_once_{username}_", "").replace(".flag", "").replace("_", " ")
            pause_state = f"⏸️ Next class ({paused_class}) will be skipped"

    # Build status message
    msg = (
        f"👤 <b>SPADA User:</b> {username}\n"
        f"📂 <b>Schedule:</b> {schedule_path if schedule_path else 'not linked'}\n"
        f"⏱️ <b>Status:</b> {pause_state}"
    )

    bot.send_message(chat_id, msg, parse_mode="HTML")
    
@bot.message_handler(commands=["setup"])
def handle_setup(message):
    chat_id = str(message.chat.id)
    if is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ you already saved credentials. use /delete first if you want to replace.")
        return
    user_states[chat_id] = "awaiting_username"
    bot.send_message(chat_id, "🟢 what's your SPADA username, darling?")

@bot.message_handler(commands=["cancel"])
def cancel(message):
    chat_id = str(message.chat.id)
    user_states.pop(chat_id, None)
    user_temp_data.pop(chat_id, None)
    waiting_upload.discard(chat_id)
    pending_csv.pop(chat_id, None)
    bot.send_message(chat_id, "❌ cancelled. i’m still here if you need me~")

@bot.message_handler(commands=["delete"])
def handle_delete(message):
    chat_id = str(message.chat.id)
    if delete_credentials(chat_id):
        bot.send_message(chat_id, "🗑️ credentials, schedule, and flags deleted. poof~")
    else:
        bot.send_message(chat_id, "⚠️ no credentials found to delete.")

@bot.message_handler(commands=["schedule"])
def handle_schedule(message):
    chat_id = str(message.chat.id)
    if not is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ run /setup first so i can link your schedule~")
        return
    bot.send_message(chat_id, "📌 manage your schedule:", reply_markup=schedule_menu_markup())
    
@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    chat_id = str(message.chat.id)
    username = find_username_by_chat(chat_id)
    if not username:
        bot.send_message(chat_id, "⚠️ no linked SPADA user found.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    # Prevent pausing indefinitely if already paused indefinitely
    if os.path.exists(flag_file):
        bot.send_message(chat_id, "⚠️ you are already paused indefinitely. Use /resume to clear it before pausing again.")
        return
    # Prevent pausing indefinitely if a pause_once flag exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        bot.send_message(chat_id, "⚠️ you have a one-time pause active. Use /resume to clear it before pausing indefinitely.")
        return
    with open(flag_file, "w") as f:
        f.write("paused")
    bot.send_message(chat_id, "⏸️ attendance paused indefinitely. use /resume to re-enable.")

@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    chat_id = str(message.chat.id)
    username = find_username_by_chat(chat_id)
    if not username:
        bot.send_message(chat_id, "⚠️ no linked SPADA user found.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    # Remove indefinite pause flag
    if os.path.exists(flag_file):
        os.remove(flag_file)
    # Remove any pause_once flags for this user
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    for f in once_flags:
        try:
            os.remove(os.path.join(FLAG_DIR, f))
        except Exception:
            pass
    bot.send_message(chat_id, "▶️ attendance resumed.")

@bot.message_handler(commands=["pauseonce"])
def cmd_pauseonce(message):
    chat_id = str(message.chat.id)
    username = find_username_by_chat(chat_id)
    schedule_path = find_schedule_path(chat_id)
    if not username or not schedule_path:
        bot.send_message(chat_id, "⚠️ no linked SPADA user or schedule.")
        return
    # Prevent pausing once if already paused indefinitely
    indefinite_flag = os.path.join(FLAG_DIR, f"pause_user_{username}.flag")
    if os.path.exists(indefinite_flag):
        bot.send_message(chat_id, "⚠️ you are already paused indefinitely. Use /resume to clear it before pausing once.")
        return
    # Prevent pausing once if a pause_once flag already exists
    once_flags = [f for f in os.listdir(FLAG_DIR) if f.startswith(f"pause_once_{username}_")]
    if once_flags:
        bot.send_message(chat_id, "⚠️ you already have a one-time pause active. Use /resume to clear it before pausing once again.")
        return
    next_class = get_next_class(schedule_path)
    if not next_class:
        bot.send_message(chat_id, "ℹ️ no upcoming class found to pause.")
        return
    flag_file = os.path.join(FLAG_DIR, f"pause_once_{username}_{next_class.replace(' ','_')}.flag")
    with open(flag_file, "w") as f:
        f.write("skip next")
    bot.send_message(chat_id, f"⏸️ next class <b>{next_class}</b> will be skipped.")


# =============================
# Setup conversation flow
# =============================
@bot.message_handler(func=lambda m: str(m.chat.id) in user_states)
def handle_conversation(message):
    chat_id = str(message.chat.id)
    text = (message.text or "").strip()
    state = user_states.get(chat_id)

    if state == "awaiting_username":
        user_temp_data[chat_id] = {"username": text}
        user_states[chat_id] = "awaiting_password"
        bot.send_message(
            chat_id,
            "🔐 what's your SPADA password?\n\n"
            "<b>warning:</b> it’s stored in plain text. use a unique password."
        )
    elif state == "awaiting_password":
        user_temp_data[chat_id]["password"] = text
        save_to_env(chat_id, user_temp_data[chat_id])
        user_states.pop(chat_id, None)
        user_temp_data.pop(chat_id, None)
        bot.send_message(chat_id, "✅ credentials saved!")
        # gentle reminder to upload schedule
        bot.send_message(chat_id, "💡 don’t forget to upload your schedule with <b>/schedule</b> → <i>Upload Schedule</i>.")

# =============================
# Photo handling (Upload flow)
# =============================

# Handle photo upload (image schedule)
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = str(message.chat.id)

    # only accept a photo if user pressed "Upload Schedule" first
    if chat_id not in waiting_upload:
        return

    if not is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ run /setup first before sending your schedule.")
        return

    # get highest-resolution photo
    file_info = bot.get_file(message.photo[-1].file_id)
    image_bytes = bot.download_file(file_info.file_path)

    bot.send_message(chat_id, "⏳ processing your schedule image with Gemini… hold tight~")

    try:
        csv_text = parse_schedule_with_gemini(image_bytes)
        if not csv_text:
            bot.send_message(chat_id, "❌ i couldn't read any schedule from that image. try a clearer shot?")
            return

        pending_csv[chat_id] = csv_text
        waiting_upload.discard(chat_id)

        # send CSV preview as a file with Save/Cancel buttons
        csv_file = io.BytesIO(csv_text.encode("utf-8"))
        csv_file.name = "schedule_preview.csv"
        bot.send_document(chat_id, csv_file, caption="📄 here’s what i extracted. save it?", reply_markup=confirm_menu_markup())

    except Exception as e:
        waiting_upload.discard(chat_id)
        bot.send_message(chat_id, f"❌ error parsing schedule: <code>{e}</code>")

# Handle CSV upload
@bot.message_handler(content_types=["document"])
def handle_csv_upload(message):
    chat_id = str(message.chat.id)
    # Only accept CSV if user pressed "Upload CSV" first
    if f"csv_{chat_id}" not in waiting_upload:
        return
    if not is_chat_id_exist(chat_id):
        bot.send_message(chat_id, "⚠️ run /setup first before sending your schedule.")
        return
    doc = message.document
    if not doc.file_name.lower().endswith(".csv"):
        bot.send_message(chat_id, "⚠️ please upload a CSV file.")
        return
    try:
        file_info = bot.get_file(doc.file_id)
        csv_bytes = bot.download_file(file_info.file_path)
        csv_text = csv_bytes.decode("utf-8")
        # Basic validation: must have header and at least one row
        lines = [l for l in csv_text.strip().splitlines() if l.strip()]
        if not lines or not lines[0].lower().startswith("coursename,day,time"):
            bot.send_message(chat_id, "❌ CSV must start with header: CourseName,Day,Time")
            waiting_upload.discard(f"csv_{chat_id}")
            return
        if len(lines) < 2:
            bot.send_message(chat_id, "❌ CSV must have at least one schedule row.")
            waiting_upload.discard(f"csv_{chat_id}")
            return
        schedule_path = find_schedule_path(chat_id)
        if not schedule_path:
            bot.send_message(chat_id, "❌ could not locate your schedule file in .env.")
            waiting_upload.discard(f"csv_{chat_id}")
            return
        with open(schedule_path, "w", encoding="utf-8") as f:
            f.write(csv_text if csv_text.endswith("\n") else csv_text + "\n")
        bot.send_message(chat_id, f"✅ schedule CSV uploaded and saved to <code>{schedule_path}</code>")
        waiting_upload.discard(f"csv_{chat_id}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ error processing CSV: <code>{e}</code>")
        waiting_upload.discard(f"csv_{chat_id}")

# =============================
# Callback handlers (Inline buttons)
# =============================
@bot.callback_query_handler(func=lambda c: c.data in ["sch_upload", "sch_upload_csv", "sch_view", "sch_delete", "sch_save", "sch_cancel"])
def handle_schedule_buttons(call: types.CallbackQuery):
    chat_id = str(call.message.chat.id)
    data = call.data

    # ensure user is set up
    if not is_chat_id_exist(chat_id):
        bot.answer_callback_query(call.id, "Please run /setup first.")
        return


    # Upload image request
    if data == "sch_upload":
        waiting_upload.add(chat_id)
        pending_csv.pop(chat_id, None)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Ready for your image!")
        bot.send_message(chat_id, "🖼 please send me your <b>schedule image</b> now.")

    # Upload CSV request
    elif data == "sch_upload_csv":
        waiting_upload.add(f"csv_{chat_id}")
        pending_csv.pop(chat_id, None)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Ready for your CSV!")
        bot.send_message(chat_id, "⬆️ please send me your <b>CSV schedule file</b> now.")

    # View current schedule
    elif data == "sch_view":
        schedule_path = find_schedule_path(chat_id)
        if not schedule_path or not os.path.exists(schedule_path) or os.path.getsize(schedule_path) == 0:
            bot.answer_callback_query(call.id, "No schedule saved yet.")
            return
        bot.answer_callback_query(call.id, "Sending your current schedule.")
        bot.send_document(chat_id, open(schedule_path, "rb"), caption="📄 your current saved schedule.")

    # Delete schedule
    elif data == "sch_delete":
        schedule_path = find_schedule_path(chat_id)
        if schedule_path and os.path.exists(schedule_path):
            try:
                os.remove(schedule_path)
                # recreate empty file to keep path valid
                open(schedule_path, "w", encoding="utf-8").close()
                bot.answer_callback_query(call.id, "Schedule deleted.")
                bot.send_message(chat_id, "🗑 schedule deleted.")
            except Exception as e:
                bot.answer_callback_query(call.id, "Failed to delete.")
                bot.send_message(chat_id, f"❌ couldn't delete: <code>{e}</code>")
        else:
            bot.answer_callback_query(call.id, "No schedule to delete.")

    # Save parsed CSV
    elif data == "sch_save":
        csv_text = pending_csv.get(chat_id)
        if not csv_text:
            bot.answer_callback_query(call.id, "Nothing to save.")
            return
        schedule_path = find_schedule_path(chat_id)
        if not schedule_path:
            bot.answer_callback_query(call.id, "No schedule path in .env.")
            bot.send_message(chat_id, "❌ couldn't locate your SCHEDULE_FILE in .env.")
            return
        try:
            with open(schedule_path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            pending_csv.pop(chat_id, None)
            bot.answer_callback_query(call.id, "Saved!")
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            bot.send_message(chat_id, f"✅ schedule saved to <code>{schedule_path}</code>")
        except Exception as e:
            bot.answer_callback_query(call.id, "Save failed.")
            bot.send_message(chat_id, f"❌ failed to save: <code>{e}</code>")

    # Cancel parsed CSV
    elif data == "sch_cancel":
        pending_csv.pop(chat_id, None)
        waiting_upload.discard(chat_id)
        bot.answer_callback_query(call.id, "Cancelled.")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, "❌ schedule upload cancelled. you can try again via <b>/schedule</b>.")

# =============================
# Run
# =============================
if __name__ == "__main__":
    bot.infinity_polling()
