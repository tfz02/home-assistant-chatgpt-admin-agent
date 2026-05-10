FROM ghcr.io/home-assistant/amd64-base:latest

RUN apk add --no-cache python3 py3-pip jq bash grep sed coreutils curl unzip

WORKDIR /app

COPY requirements.txt /app/
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

RUN curl -L https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip -o /tmp/ngrok.zip \
    && unzip /tmp/ngrok.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/ngrok \
    && rm /tmp/ngrok.zip

COPY app.py /app/
COPY run.sh /app/
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
