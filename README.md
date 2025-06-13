# Automatic Attendance Script (Work in Progress)

Easily automate your attendance process with this script.

---

## Getting Started

### 1. Install Dependencies

1. Install the dependencies using pip:
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

    SPADA_USERNAME_2=another_username
    SPADA_PASSWORD_2=another_password
    TELEGRAM_CHAT_ID_2=another_telegram_chat_id
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

---

### 3. Add Your Schedule

1. Create a CSV file (e.g., `schedule.csv`) with the following format:

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

2. Place the CSV file in the root folder of the project.

---

### 4. Telegram Notifications

This script sends notifications to your Telegram account using a bot.

- Each user must have their own `TELEGRAM_CHAT_ID` in the `.env` file.
- The script will send messages for attendance status and errors.

---

**Note:**  
This project is still under development. Contributions and feedback are welcome!