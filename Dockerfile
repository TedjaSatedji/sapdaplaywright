FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (and their OS dependencies)
# We only install Chromium to keep the image smaller, adjust if you need others
RUN playwright install --with-deps chromium

# Copy the rest of the project files
COPY . .

# Default command (can be overridden by docker-compose)
CMD ["python", "telegbot.py"]
