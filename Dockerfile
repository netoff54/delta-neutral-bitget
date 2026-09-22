FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure data directory exists for persistent volume mounting
RUN mkdir -p /app/data

# Default start command for autonomous trade loop
CMD ["python", "main.py", "--auto-trade"]
