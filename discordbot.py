import os
import asyncio
from dotenv import load_dotenv
import discord
from discord import app_commands

import store

# ==========================
# Boot
# ==========================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ==========================
# State (upload flows)
# ==========================
waiting_upload = set()  # user_ids expecting an upload (plain = image, "csv_{id}" = CSV)
pending_csv = {}        # user_id -> csv text awaiting Save/Cancel
active_flows = set()    # user_ids inside a /setup or /credsedit wait_for flow (/cancel aborts them)

HELP_TEXT = (
    "👋 hi! here's what i can do:\n\n"
    "🆕 new here? **/tutorial** walks you through setup\n\n"
    "👤 **account**\n"
    "• **/setup** – link your SPADA account\n"
    "• **/mystatus** – account, schedule & pause status\n"
    "• **/credscheck** – verify saved credentials\n"
    "• **/credsedit** – update username/password\n"
    "• **/delete** – remove your account\n\n"
    "⏱️ **attendance**\n"
    "• **/pause** – pause attendance indefinitely\n"
    "• **/pauseonce** – skip a class (you pick which)\n"
    "• **/resume** – clear any pause\n\n"
    "📅 **schedule**\n"
    "• **/schedule** – upload, view or delete your schedule\n\n"
    "🧹 **/cancel** – cancel whatever's in progress"
)

TUTORIAL_STEPS = [
    "👋 **welcome!**\n\n"
    "i'm your SPADA attendance buddy — i automatically submit your class attendance "
    "and message you the result here.\n\n"
    "setup takes ~2 minutes. tap **next ▶** to get started.",

    "1️⃣ **link your account**\n\n"
    "send /setup and answer my two questions:\n"
    "• your SPADA username/NIM\n"
    "• your password\n\n"
    "⚠️ passwords are stored in plain text — use a unique one.",

    "2️⃣ **upload your schedule**\n\n"
    "send /schedule, then pick one:\n"
    "• 🖼 **Upload Schedule Image** — snap a photo of your schedule, i'll read it with Gemini ✨\n"
    "• ⬆️ **Upload CSV** — already have a CSV? even faster.",

    "3️⃣ **you're done!**\n\n"
    "when a class starts, i attend it for you and message you the result here. "
    "nothing to do on your end.\n\n"
    "check /mystatus anytime to see your next class.",

    "4️⃣ **need a break?**\n\n"
    "• /pauseonce — skip one class (you pick which)\n"
    "• /pause — stop attending until you say so\n"
    "• /resume — back on duty\n\n"
    "that's everything. you're all set! 🎉",
]


def find_user(interaction: discord.Interaction) -> store.UserRecord | None:
    return store.find_user("discord", str(interaction.user.id))


def schedule_preview_text(csv_text: str, title: str = "here's what i extracted — save it?") -> str:
    """Readable numbered list of schedule rows for chat display (Discord markdown)."""
    rows = store.parse_schedule_text(csv_text)
    header = f"📄 **{title}** ({len(rows)} classes):\n\n"
    lines = []
    for i, row in enumerate(rows, start=1):
        line = f"{i}. **{row['CourseName']}** · {row['Day']} {row['Time']}"
        if sum(map(len, lines)) + len(line) > 1700:  # stay under Discord's 2000 char limit
            lines.append(f"…and {len(rows) - len(lines)} more")
            break
        lines.append(line)
    return header + "\n".join(lines)


# ==========================
# UI Views
# ==========================
class ScheduleMenu(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=180)
        self.user_id = user_id

    @discord.ui.button(label="🖼 Upload Schedule Image", style=discord.ButtonStyle.primary)
    async def upload_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(self.user_id)
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("🖼 send your schedule image now (png/jpg).", ephemeral=True)

    @discord.ui.button(label="⬆️ Upload CSV", style=discord.ButtonStyle.success)
    async def upload_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        waiting_upload.add(f"csv_{self.user_id}")
        pending_csv.pop(self.user_id, None)
        await interaction.response.send_message("⬆️ send your CSV schedule file now.", ephemeral=True)

    @discord.ui.button(label="📄 View Schedule", style=discord.ButtonStyle.secondary)
    async def view_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = store.find_user("discord", self.user_id)
        if not user or not store.load_schedule_rows(user.schedule_path):
            await interaction.response.send_message("📭 no schedule saved yet — upload one first!", ephemeral=True)
            return
        with open(user.schedule_path, encoding="utf-8") as f:
            csv_text = f.read()
        await interaction.response.send_message(
            schedule_preview_text(csv_text, "your current schedule"),
            file=discord.File(user.schedule_path),
            ephemeral=True,
        )

    @discord.ui.button(label="🗑 Delete Schedule", style=discord.ButtonStyle.danger)
    async def delete_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = store.find_user("discord", self.user_id)
        if user and os.path.exists(user.schedule_path):
            # keep an empty file behind so the .env path stays valid for spda.py
            store.delete_schedule_file(user.schedule_path, recreate_empty=True)
            await interaction.response.send_message("🗑 schedule deleted. upload a new one anytime.", ephemeral=True)
        else:
            await interaction.response.send_message("📭 no schedule to delete.", ephemeral=True)


class ConfirmMenu(discord.ui.View):
    def __init__(self, user_id: str, csv_text: str):
        super().__init__(timeout=240)
        self.user_id = user_id
        self.csv_text = csv_text

    @discord.ui.button(label="✅ Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = store.find_user("discord", self.user_id)
        if not user:
            await interaction.response.send_message("❌ couldn't find your account — run /setup again.", ephemeral=True)
            return
        try:
            store.save_schedule_csv(user.schedule_path, self.csv_text)
            pending_csv.pop(self.user_id, None)
            await interaction.response.edit_message(
                content="✅ schedule saved! check it anytime via /schedule → 📄 View Schedule.", view=None
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ failed to save: `{e}`", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_csv.pop(self.user_id, None)
        waiting_upload.discard(self.user_id)
        await interaction.response.edit_message(content="❌ schedule upload cancelled — /schedule to try again.", view=None)


class DeleteConfirmView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="🗑 Delete everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = store.find_user("discord", self.user_id)
        if user and store.delete_user(user):
            await interaction.response.edit_message(
                content="🗑️ credentials, schedule, and flags deleted. sad to see you go — `/setup` anytime to come back.",
                view=None,
            )
        else:
            await interaction.response.edit_message(content="🤔 nothing left to delete.", view=None)

    @discord.ui.button(label="❌ Keep my account", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="phew 😌 nothing was deleted.", view=None)


class TutorialView(discord.ui.View):
    def __init__(self, step: int = 0):
        super().__init__(timeout=300)
        self.step = step
        self._sync_buttons()

    def _sync_buttons(self):
        last = len(TUTORIAL_STEPS) - 1
        self.prev_button.disabled = self.step == 0
        self.next_button.label = "next ▶" if self.step < last else "✅ done"

    @discord.ui.button(label="◀ prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.step = max(0, self.step - 1)
        self._sync_buttons()
        await interaction.response.edit_message(content=TUTORIAL_STEPS[self.step], view=self)

    @discord.ui.button(label="next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.step >= len(TUTORIAL_STEPS) - 1:
            await interaction.response.edit_message(content="🎉 that's everything! type /help anytime.", view=None)
            return
        self.step += 1
        self._sync_buttons()
        await interaction.response.edit_message(content=TUTORIAL_STEPS[self.step], view=self)

    @discord.ui.button(label="📚 all commands", style=discord.ButtonStyle.secondary)
    async def all_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


class SkipClassView(discord.ui.View):
    def __init__(self, user_id: str, courses: list[str]):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.courses = courses

        select = discord.ui.Select(
            placeholder="pick the class to skip…",
            options=[
                discord.SelectOption(label=course[:100], value=str(i))
                for i, course in enumerate(courses[:25])
            ],
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        course = self.courses[int(interaction.data["values"][0])]
        user = store.find_user("discord", self.user_id)

        if not user:
            await interaction.response.edit_message(content="⚠️ your account is gone — run /setup first.", view=None)
            return

        state, _ = store.get_pause_state(user.username)
        if state != "active":
            await interaction.response.edit_message(
                content="⚠️ a pause is already active — use /resume first.", view=None
            )
            return

        store.set_once_pause(user.username, course)
        await interaction.response.edit_message(
            content=f"⏭️ got it — **{course}** will be skipped. `/resume` to undo.", view=None
        )


# ==========================
# Slash commands
# ==========================
@tree.command(name="start", description="Show help / available commands")
async def start(interaction: discord.Interaction):
    await help_command.callback(interaction)


@tree.command(name="help", description="Show a help message with available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


@tree.command(name="tutorial", description="Quick start walkthrough (~2 minutes)")
async def tutorial(interaction: discord.Interaction):
    await interaction.response.send_message(TUTORIAL_STEPS[0], view=TutorialView(0), ephemeral=True)


@tree.command(name="setup", description="Link your SPADA account (guided)")
async def setup(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if find_user(interaction):
        await interaction.response.send_message(
            "⚠️ you're already linked! use /credsedit to update, or /mystatus to check.", ephemeral=True
        )
        return

    def check(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    active_flows.add(user_id)
    await interaction.response.send_message(
        "🟢 what's your SPADA username/NIM?\n\n(you can /cancel anytime)", ephemeral=True
    )

    try:
        msg_u = await client.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        active_flows.discard(user_id)
        await interaction.followup.send("❌ timed out — run /setup again when ready.", ephemeral=True)
        return

    if user_id not in active_flows:
        await interaction.followup.send("❌ setup cancelled.", ephemeral=True)
        return

    username = msg_u.content.strip()
    await interaction.followup.send(
        "🔐 what's your SPADA password?\n\n"
        "⚠️ **warning:** it's stored in plain text. use a unique password.",
        ephemeral=True,
    )

    try:
        msg_p = await client.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        active_flows.discard(user_id)
        await interaction.followup.send("❌ timed out — run /setup again when ready.", ephemeral=True)
        return

    if user_id not in active_flows:
        await interaction.followup.send("❌ setup cancelled.", ephemeral=True)
        return
    active_flows.discard(user_id)

    store.save_user("discord", user_id, username, msg_p.content.strip())

    await interaction.followup.send("✅ credentials saved!", ephemeral=True)
    await interaction.followup.send(
        "💡 don't forget to upload your schedule with /schedule → 🖼 Upload Schedule Image.", ephemeral=True
    )


@tree.command(name="credscheck", description="Verify your saved SPADA credentials")
async def credscheck(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no saved credentials found. run /setup first.", ephemeral=True)
        return

    await interaction.response.send_message("🔎 checking your saved SPADA credentials...", ephemeral=True)
    is_valid, result_message = store.verify_spada_credentials(user.username, user.password)
    await interaction.followup.send((f"✅ {result_message}" if is_valid else f"❌ {result_message}"), ephemeral=True)


@tree.command(name="credsedit", description="Update your saved SPADA username/password")
async def credsedit(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no saved credentials found. run /setup first.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    current_username = user.username

    def check(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    active_flows.add(user_id)
    await interaction.response.send_message(
        f"🟡 your current SPADA username/NIM is **{current_username}**.\n\n"
        "send your new username/NIM now.\n\n(you can /cancel anytime)",
        ephemeral=True,
    )

    try:
        msg_u = await client.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        active_flows.discard(user_id)
        await interaction.followup.send("❌ timed out — run /credsedit again when ready.", ephemeral=True)
        return

    if user_id not in active_flows:
        await interaction.followup.send("❌ update cancelled.", ephemeral=True)
        return

    new_username = msg_u.content.strip()
    await interaction.followup.send(
        "🔐 send your new SPADA password now.\n\n"
        "⚠️ **warning:** it's stored in plain text. use a unique password.",
        ephemeral=True,
    )

    try:
        msg_p = await client.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        active_flows.discard(user_id)
        await interaction.followup.send("❌ timed out — run /credsedit again when ready.", ephemeral=True)
        return

    if user_id not in active_flows:
        await interaction.followup.send("❌ update cancelled.", ephemeral=True)
        return
    active_flows.discard(user_id)

    if store.update_user(user, new_username, msg_p.content.strip()):
        await interaction.followup.send("✅ credentials updated.", ephemeral=True)
    else:
        await interaction.followup.send(
            "❌ couldn't update your credentials. try /setup if the record is missing.", ephemeral=True
        )


@tree.command(name="mystatus", description="Show linked SPADA account info and pause state")
async def mystatus(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no linked SPADA user found. run /setup first.", ephemeral=True)
        return

    state, skipped_course = store.get_pause_state(user.username)
    status_line = {
        "active": "▶️ active",
        "indefinite": "⏸️ paused indefinitely (use /resume)",
    }.get(state, f"⏭️ **{skipped_course}** will be skipped (use /resume to undo)")

    classes = store.upcoming_classes(user.schedule_path)
    if classes:
        nxt = classes[0]
        next_line = f"🕐 **next class:** {nxt['course']} · {nxt['day']} {nxt['time']}"
    else:
        next_line = "🕐 **next class:** none scheduled 🎉"

    n_classes = len(store.load_schedule_rows(user.schedule_path))
    schedule_line = (
        f"📅 **schedule:** {n_classes} classes" if n_classes
        else "📅 **schedule:** empty — add one via /schedule"
    )

    await interaction.response.send_message(
        f"👤 **SPADA user:** {user.username}\n"
        f"{schedule_line}\n"
        f"{next_line}\n"
        f"⏱️ **status:** {status_line}",
        ephemeral=True,
    )


@tree.command(name="pause", description="Pause attendance indefinitely")
async def pause(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no linked SPADA user. run /setup first.", ephemeral=True)
        return

    state, _ = store.get_pause_state(user.username)
    if state == "indefinite":
        await interaction.response.send_message("⚠️ you're already paused indefinitely. /resume to clear it first.", ephemeral=True)
        return
    if state == "once":
        await interaction.response.send_message("⚠️ you have a one-time skip active. /resume to clear it first.", ephemeral=True)
        return

    store.set_indefinite_pause(user.username)
    await interaction.response.send_message("⏸️ attendance paused indefinitely. /resume to re-enable.", ephemeral=True)


@tree.command(name="resume", description="Resume attendance if paused")
async def resume(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no linked SPADA user. run /setup first.", ephemeral=True)
        return

    store.clear_all_pauses(user.username)
    await interaction.response.send_message("▶️ attendance resumed. i'm on duty again ✅", ephemeral=True)


@tree.command(name="pauseonce", description="Skip attendance for one upcoming class (you pick which)")
async def pauseonce(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no linked SPADA user. run /setup first.", ephemeral=True)
        return

    state, _ = store.get_pause_state(user.username)
    if state == "indefinite":
        await interaction.response.send_message("⚠️ you're paused indefinitely. /resume to clear it first.", ephemeral=True)
        return
    if state == "once":
        await interaction.response.send_message("⚠️ you already have a one-time skip active. /resume to clear it first.", ephemeral=True)
        return

    classes = store.upcoming_classes(user.schedule_path)
    if not classes:
        await interaction.response.send_message(
            "📭 no upcoming classes in your schedule. add one via /schedule.", ephemeral=True
        )
        return

    courses = [c["course"] for c in classes[:25]]
    await interaction.response.send_message(
        "⏭️ which class should i skip?",
        view=SkipClassView(str(interaction.user.id), courses),
        ephemeral=True,
    )


@tree.command(name="delete", description="Remove your saved credentials (also deletes schedule & pause flags)")
async def delete(interaction: discord.Interaction):
    user = find_user(interaction)
    if not user:
        await interaction.response.send_message("⚠️ no credentials found to delete.", ephemeral=True)
        return
    await interaction.response.send_message(
        "⚠️ this deletes your SPADA credentials, schedule, and pause flags.\n\nare you sure?",
        view=DeleteConfirmView(str(interaction.user.id)),
        ephemeral=True,
    )


@tree.command(name="schedule", description="Manage your class schedule (upload/view/delete)")
async def schedule(interaction: discord.Interaction):
    if not find_user(interaction):
        await interaction.response.send_message("⚠️ run /setup first so i can link your schedule~", ephemeral=True)
        return
    await interaction.response.send_message(
        "📌 manage your schedule:", view=ScheduleMenu(str(interaction.user.id)), ephemeral=True
    )


@tree.command(name="cancel", description="Cancel the current setup or pending upload")
async def cancel(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    active_flows.discard(user_id)
    waiting_upload.discard(user_id)
    waiting_upload.discard(f"csv_{user_id}")
    pending_csv.pop(user_id, None)
    await interaction.response.send_message("❌ cancelled.", ephemeral=True)


# ==========================
# Handle uploads (image / CSV flows)
# ==========================
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    user_id = str(message.author.id)

    # ---- CSV upload ----
    if f"csv_{user_id}" in waiting_upload:
        if not message.attachments:
            return
        attachment = message.attachments[0]
        if not attachment.filename.lower().endswith(".csv"):
            await message.channel.send("⚠️ that's not a CSV file. send a .csv schedule.", delete_after=8)
            return
        waiting_upload.discard(f"csv_{user_id}")
        try:
            csv_text = (await attachment.read()).decode("utf-8")

            error = store.validate_schedule_csv(csv_text)
            if error:
                await message.channel.send(f"❌ {error}", delete_after=10)
                return

            user = store.find_user("discord", user_id)
            if not user:
                await message.channel.send("⚠️ run /setup first before sending your schedule.", delete_after=10)
                return

            store.save_schedule_csv(user.schedule_path, csv_text)
            n = len(store.parse_schedule_text(csv_text))
            await message.channel.send(f"✅ schedule saved — {n} classes on the list! check it via /schedule → 📄 View Schedule.")
        except Exception as e:
            await message.channel.send(f"❌ error processing CSV: `{e}`")
        return

    # ---- image upload ----
    if user_id not in waiting_upload:
        if message.attachments and any(
            a.filename.lower().endswith((".jpg", ".jpeg", ".png")) for a in message.attachments
        ):
            await message.channel.send(
                "👀 i see an image! if that's your schedule, press /schedule → 🖼 Upload Schedule Image first "
                "so i know what to do with it.",
                delete_after=12,
            )
        return

    attachment = next(
        (a for a in message.attachments if a.filename.lower().endswith((".jpg", ".jpeg", ".png"))), None
    )
    if not attachment:
        await message.channel.send("⚠️ please upload an image file (png/jpg).", delete_after=8)
        return

    waiting_upload.discard(user_id)
    await message.channel.send("⏳ reading your schedule with Gemini...", delete_after=6)

    try:
        csv_text = store.parse_schedule_with_gemini(await attachment.read())
        if not csv_text:
            await message.channel.send("❌ i couldn't read any schedule from that image. try a clearer shot?", delete_after=8)
            return

        csv_text = store.normalize_schedule_csv(csv_text)
        error = store.validate_schedule_csv(csv_text)
        if error:
            await message.channel.send(f"❌ the extracted schedule doesn't look right — {error}", delete_after=10)
            return

        pending_csv[user_id] = csv_text
        await message.channel.send(schedule_preview_text(csv_text), view=ConfirmMenu(user_id, csv_text))
    except Exception as e:
        await message.channel.send(f"❌ error parsing schedule: `{e}`")


# ==========================
# Boot & sync
# ==========================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
