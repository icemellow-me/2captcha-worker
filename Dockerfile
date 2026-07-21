FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data static

ENV DASHBOARD_PASSWORD=admin
ENV DASHBOARD_PORT=8890
ENV CAPTCHA_KEY=ec63b74d6ee7848c14b01cc436c6eb21
ENV SOLVER_UNIVERSAL=http://172.17.0.1:8855
ENV SOLVER_TURNSTILE=http://172.17.0.1:8878
ENV SOLVER_RECAPTCHA=http://172.17.0.1:8866
ENV POLL_INTERVAL=1.0

EXPOSE 8890

CMD ["python3", "server.py"]
