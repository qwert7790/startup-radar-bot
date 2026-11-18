FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py local_translation.py .
RUN python local_translation.py
RUN mkdir -p /app/data
CMD ["python", "bot.py"]
