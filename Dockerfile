FROM python:3.11-slim

WORKDIR /app

# Create a dedicated non-root user to run the relay
RUN groupadd -r relay && useradd -r -g relay -d /app relay

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay.py .

# The relay listens on port 25 inside the container.
# Map it to whatever host port you need in docker-compose.yml.
EXPOSE 25

# Drop root privileges
USER relay

CMD ["python", "relay.py"]
