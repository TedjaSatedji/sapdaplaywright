import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ENV_FILE = ".env"

intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)

user_states = {}
user_temp_data = {}

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    try:
        synced = await client.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Sync failed: {e}")

@client.tree.command(name="start", description="Show help message with all available commands.")
async def start_command(interaction: discord.Interaction):
    commands_text = (
        "🤖 Available commands:\n"
        "/start - Show this help message\n"
        "/me - Check your saved SPADA data\n"
        "/setup - Start SPADA setup\n"
        "/delete - Delete your saved credentials\n"
        "/cancel - Cancel current setup"
    )
    await interaction.response.send_message(commands_text, ephemeral=True)

@client.tree.command(name="me", description="Check for existing SPADA credentials.")
async def me_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if not os.path.exists(ENV_FILE):
        await interaction.response.send_message("ℹ️ No credentials found for your account.", ephemeral=True)
        return
    with open(ENV_FILE, "r") as f:
        lines = f.readlines()
    username = None
    for i in range(len(lines)):
        if lines[i].startswith("DISCORD_USER_ID_") and user_id in lines[i]:
            if i >= 2 and lines[i-2].startswith("SPADA_USERNAME_"):
                username = lines[i-2].split("=", 1)[1].strip()
            break
    if username:
        await interaction.response.send_message(f"👤 Your SPADA username: `{username}`", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ No credentials found for your account.", ephemeral=True)

@client.tree.command(name="setup", description="Save your SPADA credentials.")
async def setup_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if is_user_id_exist(user_id):
        await interaction.response.send_message("⚠️ You already have credentials saved. Use /delete to remove them first.", ephemeral=True)
        return
    user_states[user_id] = "awaiting_username"
    await interaction.user.send("🟢 What is your SPADA username?")
    await interaction.response.send_message("✅ Check your DM to continue setup.", ephemeral=True)

@client.tree.command(name="cancel", description="Cancel current setup.")
async def cancel_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_states.pop(user_id, None)
    user_temp_data.pop(user_id, None)
    await interaction.response.send_message("❌ Setup cancelled.", ephemeral=True)

@client.tree.command(name="delete", description="Delete your saved SPADA credentials.")
async def delete_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if delete_credentials(user_id):
        await interaction.response.send_message("🗑️ Your credentials have been deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No credentials found for your account.", ephemeral=True)

@client.event
async def on_message(message):
    user_id = str(message.author.id)
    if message.author == client.user:
        return
    if isinstance(message.channel, discord.DMChannel) and user_id in user_states:
        text = message.content.strip()
        state = user_states[user_id]
        if state == "awaiting_username":
            user_temp_data[user_id] = {"username": text}
            user_states[user_id] = "awaiting_password"
            await message.channel.send(
                "🔐 What is your SPADA password?\n\n⚠️ It will be stored in plain text. Use a unique password."
            )
        elif state == "awaiting_password":
            user_temp_data[user_id]["password"] = text
            save_to_env(user_id, user_temp_data[user_id])
            await message.channel.send("✅ Credentials saved successfully!")
            user_states.pop(user_id)
            user_temp_data.pop(user_id)

    await client.process_commands(message)

def is_user_id_exist(user_id):
    if not os.path.exists(ENV_FILE):
        return False
    with open(ENV_FILE, "r") as f:
        return any("DISCORD_USER_ID_" in line and user_id in line for line in f)

def get_next_index():
    if not os.path.exists(ENV_FILE):
        return 1
    with open(ENV_FILE, "r") as f:
        indices = [int(line.split("_")[-1].split("=")[0]) for line in f if line.startswith("SPADA_USERNAME_")]
        return max(indices) + 1 if indices else 1

def save_to_env(user_id, creds):
    index = get_next_index()
    schedule_dir = "schedules"
    os.makedirs(schedule_dir, exist_ok=True)
    schedule_path = f"{schedule_dir}/schedule_{index}.csv"
    if not os.path.exists(schedule_path):
        open(schedule_path, "w").close()
    with open(ENV_FILE, "a") as f:
        f.write(f"#--- {creds['username']} ---\n")
        f.write(f"SPADA_USERNAME_{index}={creds['username']}\n")
        f.write(f"SPADA_PASSWORD_{index}={creds['password']}\n")
        f.write(f"DISCORD_USER_ID_{index}={user_id}\n")
        f.write(f"SCHEDULE_FILE_{index}={schedule_path}\n")

def delete_credentials(user_id):
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
            and lines[i+3].startswith("DISCORD_USER_ID_")
            and lines[i+4].startswith("SCHEDULE_FILE_")
            and user_id in lines[i+3]
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

client.run(DISCORD_TOKEN)
