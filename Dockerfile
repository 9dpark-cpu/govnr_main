# Pull official, highly stable Python 3.11 runtime from Docker Hub
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Expose the exact port DigitalOcean requires
EXPOSE 8080

# The deterministic run command (WITH THE REQUIRED SPACE)
CMD["streamlit", "run", "app.py", "--server.port", "8080", "--server.address", "0.0.0.0"]
