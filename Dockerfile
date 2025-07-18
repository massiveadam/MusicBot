FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    ffmpeg \
    build-essential \
    python3-dev \
    libjpeg-dev \
    zlib1g-dev \
    libffi-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install mp4decrypt from Bento4 and MP4Box
RUN curl -L "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-639.x86_64-unknown-linux.zip" -o /tmp/bento4.zip && \
    unzip -q /tmp/bento4.zip -d /tmp && \
    install -m 0755 /tmp/Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt /usr/local/bin/mp4decrypt && \
    install -m 0755 /tmp/Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4box /usr/local/bin/MP4Box && \
    rm -rf /tmp/bento4.zip /tmp/Bento4-SDK-1-6-0-639.x86_64-unknown-linux

# Install gamdl
RUN pip install gamdl

# Install beets with plugins
RUN pip install beets[web] \
    beets-extrafiles \
    requests \
    beautifulsoup4 \
    pylast

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium && \
    playwright install-deps chromium

# Copy application files
COPY . .

# Create necessary directories
RUN mkdir -p /downloads /music /app/.beets

# Copy beets configuration
COPY beets-config.yaml /app/.beets/config.yaml

# Set environment variables
ENV BEETSDIR=/app/.beets
ENV PYTHONUNBUFFERED=1

# Expose port for beets web interface (optional)
EXPOSE 8337

# Create startup script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]