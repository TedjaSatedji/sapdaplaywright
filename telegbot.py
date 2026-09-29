import os
import io
import html
from dotenv import load_dotenv
import telebot
from telebot import types

import store

# =============================
# Boot
# =============================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# =============================
# State
# =============================
user_states = {}        # /setup & /credsedit conversation states
user_temp_data = {}     # temp creds during those conversations
waiting_upload = set()  # chat_ids asked to upload (plain = image, "csv_{id}" = CSV)
pending_csv = {}        # chat_id -> csv text awaiting Save/Cancel
pending_skip = {}       # chat_id -> course list shown in the /pauseonce picker

HELP_TEXT = (
    "hi hi~ 💫 here's what i can do:\n\n"
    "🆕 new here? <b>/tutorial</b> walks you through setup\n\n"
    "👤 <b>account</b>\n"
    "• /setup – link your SPADA account\n"
    "• /mystatus – account, schedule & pause status\n"
    "• /credscheck – verify saved credentials\n"
    "• /credsedit – update username/password\n"
    "• /delete – remove your account\n\n"
    "⏱️ <b>attendance</b>\n"
    "• /pause – pause attendance indefinitely\n"
    "• /pauseonce – skip a class (you pick which)\n"
    "• /resume – clear any pause\n\n"
    "📅 <b>schedule</b>\n"
    "• /schedule – upload, view or delete your schedule\n\n"
    "🧹 /cancel – cancel whatever's in progress"
)

TUTORIAL_STEPS = [
    "👋 <b>welcome!</b>\n\n"
    "i'm your SPADA attendance buddy — i automatically submit your class attendance "
    "and message you the result here.\n\n"
    "setup takes ~2 minutes. tap <b>next ▶</b> to get started.",

    "1️⃣ <b>link your account</b>\n\n"
    "send /setup and answer my two questions:\n"
    "• your SPADA username/NIM\n"
    "• your password\n\n"
    "⚠️ passwords are stored in plain text — use a unique one.",

    "2️⃣ <b>upload your schedule</b>\n\n"
    "send /schedule, then pick one:\n"
    "• 🖼 <b>Upload Schedule Image</b> — snap a photo of your schedule, i'll read it with Gemini ✨\n"
    "• ⬆️ <b>Upload CSV</b> — already have a CSV? even faster.",

    "3️⃣ <b>you're done!</b>\n\n"
    "when a class starts, i attend it for you and message you the result here. "
    "nothing to do on your end.\n\n"
    "check /mystatus anytime to see your next class.",

    "4️⃣ <b>need a break?</b>\n\n"
    "• /pauseonce — skip one class (you pick which)\n"
    "• /pause — stop attending until you say so\n"
    "• /resume — back on duty\n\n"
    "that's everything. you're all set! 🎉",
]


def tutorial_markup(step: int) -> types.InlineKeyboardMarkup:
    last = len(TUTORIAL_STEPS) - 1
    row = []
    if step > 0:
        row.append(types.InlineKeyboardButton("◀ prev", callback_data=f"tut_{step - 1}"))
    if step < last:
        row.append(types.InlineKeyboardButton("next ▶", callback_data=f"tut_{step + 1}"))
    else:
        row.append(types.InlineKeyboardButton("✅ done", callback_data="tut_done"))
    row.append(types.InlineKeyboardButton("📚 all commands", callback_data="tut_help"))
    kb = types.InlineKeyboardMarkup()
    kb.add(*row)
    return kb

# =============================
# Small UI helpers
# =============================
def schedule_menu_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🖼 Upload Schedule Image", callback_data="sch_upload"),
        types.InlineKeyboardButton("⬆️ Upload CSV", callback_data="sch_upload_csv"),
        types.InlineKeyboardButton("📄 View Schedule", callback_data="sch_view"),
    )
    kb.add(types.InlineKeyboardButton("🗑 Delete Schedule", callback_data="sch_delete"))
    return kb


def confirm_menu_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Save", callback_data="sch_save"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel"),
    )
    return kb


def remove_kb(call: types.CallbackQuery):
    """Best-effort removal of an inline keyboard after a choice was made."""
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


def short_course_label(course: str, day: str, time: str) -> str:
    """Compact 'Course · Day HH:MM' label for buttons, safe for long course names."""
    text = f"{course} · {day} {time.split(' - ')[0]}"
    return text if len(text) <= 60 else text[:57] + "…"


def schedule_preview_text(csv_text: str, title: str = "here's what i extracted — save it?") -> str:
    """Readable numbered list of schedule rows for chat display (HTML-safe)."""
    rows = store.parse_schedule_text(csv_text)
    header = f"📄 <b>{title}</b> ({len(rows)} classes):\n\n"
    lines = []
    for i, row in enumerate(rows, start=1):
        line = f"{i}. <b>{html.escape(row['CourseName'])}</b> · {row['Day']} {row['Time']}"
        if sum(map(len, lines)) + len(line) > 3800:  # stay under Telegram's 4096 limit
            lines.append(f"…and {len(rows) - len(lines)} more")
            break
        lines.append(line)
    return header + "\n".join(lines)


# =============================
# Commands
# =============================
@bot.message_handler(commands=["help", "start"])
def handle_help(message):
    bot.send_message(message.chat.id, HELP_TEXT)


@bot.message_handler(commands=["tutorial"])
def cmd_tutorial(message):
    bot.send_message(message.chat.id, TUTORIAL_STEPS[0], reply_markup=tutorial_markup(0))


@bot.message_handler(commands=["mystatus"])
def cmd_mystatus(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no linked SPADA user found. run /setup first.")
        return

    state, skipped_course = store.get_pause_state(user.username)
    status_line = {
        "active": "▶️ active",
        "indefinite": "⏸️ paused indefinitely (use /resume)",
    }.get(state, f"⏭️ <b>{skipped_course}</b> will be skipped (use /resume to undo)")

    classes = store.upcoming_classes(user.schedule_path)
    if classes:
        nxt = classes[0]
        next_line = f"🕐 <b>next class:</b> {nxt['course']} · {nxt['day']} {nxt['time']}"
    else:
        next_line = "🕐 <b>next class:</b> none scheduled 🎉"

    n_classes = len(store.load_schedule_rows(user.schedule_path))
    schedule_line = (
        f"📅 <b>schedule:</b> {n_classes} classes" if n_classes
        else "📅 <b>schedule:</b> empty — add one via /schedule"
    )

    bot.send_message(
        chat_id,
        f"👤 <b>SPADA user:</b> {user.username}\n"
        f"{schedule_line}\n"
        f"{next_line}\n"
        f"⏱️ <b>status:</b> {status_line}",
    )


@bot.message_handler(commands=["setup"])
def handle_setup(message):
    chat_id = str(message.chat.id)
    if store.find_user("telegram", chat_id):
        bot.send_message(chat_id, "⚠️ you're already linked! use /credsedit to update, or /mystatus to check.")
        return
    user_states[chat_id] = "awaiting_username"
    bot.send_message(chat_id, "🟢 what's your SPADA username/NIM?\n\n(you can /cancel anytime)")


@bot.message_handler(commands=["credsedit"])
def handle_credsedit(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no saved credentials found. run /setup first.")
        return

    user_temp_data[chat_id] = {"current_username": user.username}
    user_states[chat_id] = "changing_username"
    bot.send_message(
        chat_id,
        f"🟡 your current SPADA username/NIM is <b>{user.username}</b>.\n\n"
        "send your new username/NIM now.\n\n(you can /cancel anytime)",
    )


@bot.message_handler(commands=["credscheck"])
def handle_credscheck(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no saved credentials found. run /setup first.")
        return

    bot.send_message(chat_id, "🔎 checking your saved SPADA credentials...")
    is_valid, result_message = store.verify_spada_credentials(user.username, user.password)
    bot.send_message(chat_id, (f"✅ {result_message}" if is_valid else f"❌ {result_message}"))


@bot.message_handler(commands=["delete"])
def handle_delete(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no credentials found to delete.")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🗑 Yes, delete everything", callback_data="del_yes"),
        types.InlineKeyboardButton("❌ Keep my account", callback_data="del_no"),
    )
    bot.send_message(
        chat_id,
        "⚠️ this deletes your SPADA credentials, schedule, and pause flags.\n\nare you sure?",
        reply_markup=kb,
    )


@bot.message_handler(commands=["pause"])
def cmd_pause(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no linked SPADA user. run /setup first.")
        return

    state, _ = store.get_pause_state(user.username)
    if state == "indefinite":
        bot.send_message(chat_id, "⚠️ you're already paused indefinitely. /resume to clear it first.")
        return
    if state == "once":
        bot.send_message(chat_id, "⚠️ you have a one-time skip active. /resume to clear it first.")
        return

    store.set_indefinite_pause(user.username)
    bot.send_message(chat_id, "⏸️ attendance paused indefinitely. /resume to re-enable.")


@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no linked SPADA user. run /setup first.")
        return

    store.clear_all_pauses(user.username)
    bot.send_message(chat_id, "▶️ attendance resumed. i'm on duty again ✅")


@bot.message_handler(commands=["pauseonce"])
def cmd_pauseonce(message):
    chat_id = str(message.chat.id)
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ no linked SPADA user. run /setup first.")
        return

    state, _ = store.get_pause_state(user.username)
    if state == "indefinite":
        bot.send_message(chat_id, "⚠️ you're paused indefinitely. /resume to clear it first.")
        return
    if state == "once":
        bot.send_message(chat_id, "⚠️ you already have a one-time skip active. /resume to clear it first.")
        return

    classes = store.upcoming_classes(user.schedule_path)
    if not classes:
        bot.send_message(chat_id, "📭 no upcoming classes in your schedule. add one via /schedule.")
        return

    shown = classes[:8]
    pending_skip[chat_id] = [c["course"] for c in shown]

    kb = types.InlineKeyboardMarkup()
    nxt = shown[0]
    kb.add(types.InlineKeyboardButton(
        f"⏭️ next: {short_course_label(nxt['course'], nxt['day'], nxt['time'])}",
        callback_data="po_0",
    ))
    for i, c in enumerate(shown[1:], start=1):
        kb.add(types.InlineKeyboardButton(
            short_course_label(c["course"], c["day"], c["time"]),
            callback_data=f"po_{i}",
        ))

    bot.send_message(chat_id, "⏭️ which class should i skip?", reply_markup=kb)


@bot.message_handler(commands=["schedule"])
def handle_schedule(message):
    chat_id = str(message.chat.id)
    if not store.find_user("telegram", chat_id):
        bot.send_message(chat_id, "⚠️ run /setup first so i can link your schedule~")
        return
    bot.send_message(chat_id, "📌 manage your schedule:", reply_markup=schedule_menu_markup())


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    chat_id = str(message.chat.id)
    user_states.pop(chat_id, None)
    user_temp_data.pop(chat_id, None)
    waiting_upload.discard(chat_id)
    waiting_upload.discard(f"csv_{chat_id}")
    pending_csv.pop(chat_id, None)
    pending_skip.pop(chat_id, None)
    bot.send_message(chat_id, "❌ cancelled.")


# =============================
# Setup / credsedit conversation flow
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
            "<b>warning:</b> it's stored in plain text. use a unique password.",
        )
    elif state == "awaiting_password":
        user_temp_data[chat_id]["password"] = text
        store.save_user("telegram", chat_id, user_temp_data[chat_id]["username"], text)
        user_states.pop(chat_id, None)
        user_temp_data.pop(chat_id, None)
        bot.send_message(chat_id, "✅ credentials saved!")
        bot.send_message(chat_id, "💡 don't forget to upload your schedule with /schedule → 🖼 Upload Schedule Image.")
    elif state == "changing_username":
        user_temp_data[chat_id]["new_username"] = text
        user_states[chat_id] = "changing_password"
        bot.send_message(
            chat_id,
            "🔐 send your new SPADA password now.\n\n"
            "<b>warning:</b> it's stored in plain text. use a unique password.",
        )
    elif state == "changing_password":
        user_temp_data[chat_id]["password"] = text
        user = store.find_user("telegram", chat_id)
        if user and store.update_user(user, user_temp_data[chat_id]["new_username"], text):
            bot.send_message(chat_id, "✅ credentials updated.")
        else:
            bot.send_message(chat_id, "❌ couldn't update your credentials. try /setup if the record is missing.")
        user_states.pop(chat_id, None)
        user_temp_data.pop(chat_id, None)


# =============================
# Media handling (schedule uploads)
# =============================
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = str(message.chat.id)

    if chat_id not in waiting_upload:
        bot.send_message(
            chat_id,
            "👀 i see a photo! if that's your schedule, press /schedule → 🖼 Upload Schedule Image first "
            "so i know what to do with it.",
        )
        return

    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ run /setup first before sending your schedule.")
        waiting_upload.discard(chat_id)
        return

    file_info = bot.get_file(message.photo[-1].file_id)
    image_bytes = bot.download_file(file_info.file_path)

    bot.send_message(chat_id, "⏳ reading your schedule with Gemini...")
    waiting_upload.discard(chat_id)

    try:
        csv_text = store.parse_schedule_with_gemini(image_bytes)
        if not csv_text:
            bot.send_message(chat_id, "❌ i couldn't read any schedule from that image. try a clearer shot?")
            return

        csv_text = store.normalize_schedule_csv(csv_text)
        error = store.validate_schedule_csv(csv_text)
        if error:
            bot.send_message(chat_id, f"❌ the extracted schedule doesn't look right — {error}")
            return

        pending_csv[chat_id] = csv_text
        bot.send_message(chat_id, schedule_preview_text(csv_text), reply_markup=confirm_menu_markup())
    except Exception as e:
        bot.send_message(chat_id, f"❌ error parsing schedule: <code>{e}</code>")


@bot.message_handler(content_types=["document"])
def handle_csv_upload(message):
    chat_id = str(message.chat.id)
    key = f"csv_{chat_id}"

    if key not in waiting_upload:
        doc = message.document
        if doc and doc.file_name and doc.file_name.lower().endswith(".csv"):
            bot.send_message(
                chat_id,
                "👀 got a CSV! press /schedule → ⬆️ Upload CSV first so i know where to put it.",
            )
        return

    user = store.find_user("telegram", chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ run /setup first before sending your schedule.")
        waiting_upload.discard(key)
        return

    doc = message.document
    if not doc.file_name.lower().endswith(".csv"):
        bot.send_message(chat_id, "⚠️ that's not a CSV file. send a .csv schedule.")
        return

    try:
        file_info = bot.get_file(doc.file_id)
        csv_text = bot.download_file(file_info.file_path).decode("utf-8")

        error = store.validate_schedule_csv(csv_text)
        if error:
            bot.send_message(chat_id, f"❌ {error}")
            waiting_upload.discard(key)
            return

        store.save_schedule_csv(user.schedule_path, csv_text)
        waiting_upload.discard(key)
        n = len(store.parse_schedule_text(csv_text))
        bot.send_message(chat_id, f"✅ schedule saved — {n} classes on the list! check it via /schedule → 📄 View Schedule.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ error processing CSV: <code>{e}</code>")
        waiting_upload.discard(key)


# =============================
# Callback handlers (inline buttons)
# =============================
@bot.callback_query_handler(func=lambda c: c.data.startswith(("sch_", "del_", "po_", "tut_")))
def handle_callbacks(call: types.CallbackQuery):
    chat_id = str(call.message.chat.id)
    data = call.data

    # ---- tutorial navigation ----
    if data.startswith("tut_"):
        if data == "tut_done":
            remove_kb(call)
            bot.answer_callback_query(call.id, "you're all set! 🎉")
            return
        if data == "tut_help":
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, HELP_TEXT)
            return
        try:
            step = int(data.split("_")[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id)
            return
        if 0 <= step < len(TUTORIAL_STEPS):
            try:
                bot.edit_message_text(
                    TUTORIAL_STEPS[step],
                    chat_id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=tutorial_markup(step),
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id)
        return

    # ---- /pauseonce picker ----
    if data.startswith("po_"):
        user = store.find_user("telegram", chat_id)
        courses = pending_skip.get(chat_id, [])
        try:
            course = courses[int(data.split("_")[1])]
        except (ValueError, IndexError):
            course = None

        if not user or course is None:
            remove_kb(call)
            pending_skip.pop(chat_id, None)
            bot.answer_callback_query(call.id, "this picker expired — run /pauseonce again.")
            return

        state, _ = store.get_pause_state(user.username)
        if state != "active":
            remove_kb(call)
            pending_skip.pop(chat_id, None)
            bot.answer_callback_query(call.id, "a pause is already active — use /resume first.")
            return

        store.set_once_pause(user.username, course)
        pending_skip.pop(chat_id, None)
        remove_kb(call)
        bot.answer_callback_query(call.id, "done!")
        bot.send_message(chat_id, f"⏭️ got it — <b>{course}</b> will be skipped. /resume to undo.")
        return

    # ---- /delete confirmation ----
    if data in ("del_yes", "del_no"):
        remove_kb(call)
        if data == "del_no":
            bot.answer_callback_query(call.id, "kept!")
            bot.send_message(chat_id, "phew 😌 nothing was deleted.")
            return
        user = store.find_user("telegram", chat_id)
        if user and store.delete_user(user):
            bot.answer_callback_query(call.id, "deleted.")
            bot.send_message(chat_id, "🗑️ credentials, schedule, and flags deleted. /setup anytime to come back.")
        else:
            bot.answer_callback_query(call.id, "nothing to delete.")
        return

    # ---- schedule menu (needs a linked account) ----
    user = store.find_user("telegram", chat_id)
    if not user:
        bot.answer_callback_query(call.id, "please run /setup first.")
        return

    if data == "sch_upload":
        waiting_upload.add(chat_id)
        pending_csv.pop(chat_id, None)
        remove_kb(call)
        bot.answer_callback_query(call.id, "ready for your image!")
        bot.send_message(chat_id, "🖼 please send me your <b>schedule image</b> now.")

    elif data == "sch_upload_csv":
        waiting_upload.add(f"csv_{chat_id}")
        pending_csv.pop(chat_id, None)
        remove_kb(call)
        bot.answer_callback_query(call.id, "ready for your CSV!")
        bot.send_message(chat_id, "⬆️ please send me your <b>CSV schedule file</b> now.")

    elif data == "sch_view":
        rows = store.load_schedule_rows(user.schedule_path)
        if not rows:
            bot.answer_callback_query(call.id, "no schedule saved yet.")
            return
        bot.answer_callback_query(call.id, "sending your current schedule.")
        with open(user.schedule_path, "rb") as f:
            data = f.read()
        bot.send_message(chat_id, schedule_preview_text(data.decode("utf-8"), "your current schedule"))
        doc = io.BytesIO(data)
        doc.name = "schedule.csv"
        bot.send_document(chat_id, doc, caption="📎 raw CSV backup")

    elif data == "sch_delete":
        if os.path.exists(user.schedule_path):
            store.delete_schedule_file(user.schedule_path, recreate_empty=True)
            bot.answer_callback_query(call.id, "schedule deleted.")
            bot.send_message(chat_id, "🗑 schedule deleted. upload a new one anytime.")
        else:
            bot.answer_callback_query(call.id, "no schedule to delete.")

    elif data == "sch_save":
        csv_text = pending_csv.get(chat_id)
        if not csv_text:
            bot.answer_callback_query(call.id, "nothing to save.")
            return
        try:
            store.save_schedule_csv(user.schedule_path, csv_text)
            pending_csv.pop(chat_id, None)
            remove_kb(call)
            bot.answer_callback_query(call.id, "saved!")
            bot.send_message(chat_id, "✅ schedule saved! check it anytime via /schedule → 📄 View Schedule.")
        except Exception as e:
            bot.answer_callback_query(call.id, "save failed.")
            bot.send_message(chat_id, f"❌ failed to save: <code>{e}</code>")

    elif data == "sch_cancel":
        pending_csv.pop(chat_id, None)
        waiting_upload.discard(chat_id)
        remove_kb(call)
        bot.answer_callback_query(call.id, "cancelled.")
        bot.send_message(chat_id, "❌ schedule upload cancelled. you can try again via /schedule.")


# =============================
# Run
# =============================
if __name__ == "__main__":
    # register the command palette shown in Telegram's UI (cosmetic; never block startup)
    try:
        bot.set_my_commands([
            types.BotCommand("start", "wake me up / see commands"),
            types.BotCommand("help", "what i can do"),
            types.BotCommand("tutorial", "quick start walkthrough"),
            types.BotCommand("mystatus", "your account & pause status"),
            types.BotCommand("setup", "link your SPADA account"),
            types.BotCommand("schedule", "manage your class schedule"),
            types.BotCommand("pauseonce", "skip an upcoming class"),
            types.BotCommand("pause", "pause attendance indefinitely"),
            types.BotCommand("resume", "clear any pause"),
            types.BotCommand("credscheck", "verify saved credentials"),
            types.BotCommand("credsedit", "update username/password"),
            types.BotCommand("delete", "remove your account"),
            types.BotCommand("cancel", "cancel current action"),
        ])
    except Exception:
        pass

    bot.infinity_polling()
