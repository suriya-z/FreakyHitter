FROM mcr.microsoft.com/playwright:v1.49.0-jammy

# Set work directory
WORKDIR /app

# Install pip and system requirements
RUN apt-get update && apt-get install -y python3-pip

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install -r requirements.txt

# Command to run the bot
CMD ["python3", "bot.py"]
