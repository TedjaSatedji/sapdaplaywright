# 🤖 Automatic Attendance Script (Work in Progress)

Easily automate your **SPADA attendance** process with support for **Telegram**, **Discord**, and **Gemini AI** for schedule extraction.  

---

## ✨ Features

- 👥 **Multiple user support** via `.env`, Telegram bot, or Discord bot setup.  
- 💬 **Telegram & Discord notifications** for attendance status and errors.  
- 📅 **Schedule-based attendance** using per-user CSV files.  
- 🖼 **Automatic schedule extraction** from images using **Google Gemini**.  
- ⚡ **Concurrent execution** (up to 4 users at a time).  
- 🛡 **Robust error handling** and retry logic for login and attendance.  

---

## 🚀 Getting Started

### 1️⃣ Install Dependencies
```sh
pip install -r requirements.txt
```

---

### 2️⃣ Add Your Credentials

You can set up users in three ways:  

#### **A. Manual Method**
1. Create a `.env` file in the project root.  
2. Add credentials for each user (increment numbers):  
   ```env
   SPADA_USERNAME_1=your_username
   SPADA_PASSWORD_1=your_password
   TELEGRAM_CHAT_ID_1=your_telegram_chat_id
   DISCORD_USER_ID_1=your_discord_user_id
   SCHEDULE_FILE_1=schedules/schedule_1.csv
   ```
3. Add bot tokens (once only):  
   ```env
   TELEGRAM_TOKEN=your_telegram_bot_token
   DISCORD_TOKEN=your_discord_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   ```

---

#### **B. Telegram Bot Method**
1. [Create a Telegram bot](https://core.telegram.org/bots#6-botfather).  
2. Add `TELEGRAM_TOKEN` and `GEMINI_API_KEY` to `.env`.  
3. Run the bot:
   ```sh
   python telegbot.py
   ```
4. Use these commands inside Telegram:  
   - `/setup` → link SPADA credentials  
   - `/schedule` → upload/view/delete schedule (Gemini can parse from image 📸)  
   - `/me`, `/delete`, `/cancel` as needed  

---

#### **C. Discord Bot Method**
1. [Create a Discord bot](https://discord.com/developers/applications).  
2. Add `DISCORD_TOKEN` and `GEMINI_API_KEY` to `.env`.  
3. Run the bot:
   ```sh
   python discordbot.py
   ```
4. Use these commands in Discord:  
   - `/setup` → link SPADA credentials  
   - `/schedule` → upload/view/delete schedule (Gemini can parse from image 📸)  
   - `/me`, `/delete`, `/cancel` as needed  

---

## 📅 Adding Your Schedule

You can:  
- 🖼 Upload a schedule **image** via bot → Gemini extracts CSV automatically.  
- 📄 Or edit the CSV manually in `schedules/`.  

Format:  
```csv
CourseName,Day,Time
Data Science Basics,Senin,08:15 - 10:00
Web Development,Selasa,13:00 - 15:30
Cloud Computing,Kamis,09:45 - 11:15
Machine Learning Intro,Kamis,10:30 - 12:00
```

---

## 🔔 Notifications

- 📱 If using **Telegram** → notifications arrive in your chat.  
- 💻 If using **Discord** → notifications arrive in your DMs.  

---

## ▶️ Running the Attendance Script

To run the automation:  
```sh
python spda.py
```

What happens:  
- ✅ Checks all users in `.env`  
- ⏰ Detects ongoing classes (only within **15 minutes** of start)  
- 📝 Submits attendance automatically  
- ⚡ Handles up to **4 users concurrently**  

---

## 🔒 Security Notes

- ⚠️ Passwords are stored in **plain text**. Please use unique ones.  
- 🔐 Keep `.env` private and out of version control.  

---

💡 This project is still under development — feedback & contributions are always welcome!  
