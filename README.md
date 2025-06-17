# Automatic Attendance Script (Work in Progress)

Easily automate your attendance process with this script.

---

## Features

- **Multiple user support** via `.env` or Telegram bot setup.
- **Telegram notifications** for attendance status and errors.
- **Schedule-based attendance** using per-user CSV files.
- **Concurrent execution** (up to 4 users at a time).
- **Robust error handling** and retry logic for login and attendance.

---

## Getting Started

### 1. Install Dependencies

Install the dependencies using pip:
```sh
pip install -r requirements.txt
```

---

### 2. Add Your Credentials

There are two ways to add your credentials:

#### **A. Manual Method**

1. Create a new `.env` file in the project root.
2. For each user, add the following variables (increment the number for each user):
    ```env
    SPADA_USERNAME_1=your_username
    SPADA_PASSWORD_1=your_password
    TELEGRAM_CHAT_ID_1=your_telegram_chat_id
    SCHEDULE_FILE_1=schedules/schedule_1.csv

    SPADA_USERNAME_2=another_username
    SPADA_PASSWORD_2=another_password
    TELEGRAM_CHAT_ID_2=another_telegram_chat_id
    SCHEDULE_FILE_2=schedules/schedule_2.csv
    ```
3. Add your Telegram bot token (only once):
    ```env
    TELEGRAM_TOKEN=your_telegram_bot_token
    ```

#### **B. Telegram Bot Method (Recommended)**

1. [Create a Telegram bot](https://core.telegram.org/bots#6-botfather) and get the bot token.
2. Add `TELEGRAM_TOKEN=your_telegram_bot_token` to your `.env` file.
3. Run `telegbot.py`:
    ```sh
    python telegbot.py
    ```
4. Open Telegram, start your bot, and use `/setup` to save your SPADA credentials.  
   You can use `/me` to check your data, `/delete` to remove it, and `/cancel` to cancel setup.

   - Each user will have their own schedule file created automatically in the `schedules/` folder (e.g., `schedules/schedule_1.csv`).

---

### 3. Add Your Schedule

1. Create a CSV file (e.g., `schedules/schedule_1.csv`) with the following format:

    | CourseName                | Day     | Time           |
    |---------------------------|---------|----------------|
    | Data Science Basics       | Senin   | 08:15 - 10:00  |
    | Web Development           | Selasa  | 13:00 - 15:30  |
    | Cloud Computing           | Kamis   | 09:45 - 11:15  |
    | Machine Learning Intro    | Jumat   | 10:30 - 12:00  |

    **CSV Example:**
    ```csv
    CourseName,Day,Time
    Data Science Basics,Senin,08:15 - 10:00
    Web Development,Selasa,13:00 - 15:30
    Cloud Computing,Kamis,09:45 - 11:15
    Machine Learning Intro,Jumat,10:30 - 12:00
    ```

2. Place the CSV file in the `schedules/` folder.  
   (If you use the Telegram bot, an empty file will be created for you; just fill it in.)

---

### 4. Telegram Notifications

This script sends notifications to your Telegram account using a bot.

- Each user must have their own `TELEGRAM_CHAT_ID` in the `.env` file.
- The script will send messages for attendance status and errors.

---

### 5. Running the Script

To run the attendance script:
```sh
python spda.py
```

- The script will process all users found in the `.env` file.
- It will check the schedule for each user and submit attendance if a class is currently ongoing.
- At most **4 users** will be processed concurrently.

---

### 6. Security Notes

- **Passwords are stored in plain text** in the `.env` file.  
  For better security, use unique passwords and do not reuse passwords from other services.
- The `.env` file should be kept private and not world-readable.

---

**Note:**  
This project is still under development. Contributions and feedback are welcome!