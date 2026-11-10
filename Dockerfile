FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY bot.py ./
RUN pip install --no-cache-dir .
CMD ["python", "bot.py"]

