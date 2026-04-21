FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

# Non-root user for security
RUN useradd -m monitor && chown -R monitor:monitor /app
USER monitor

CMD ["python", "-u", "monitor.py"]
