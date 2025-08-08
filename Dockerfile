FROM python:3.12-slim

# Set environment variables for non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Update package lists and install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    ffmpeg \
    build-essential \
    python3-dev \
    libjpeg-dev \
    zlib1g-dev \
    libffi-dev \
    libssl-dev \
    pkg-config \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install MP4Box via package manager (simpler and more reliable)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gpac \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install mp4decrypt (Bento4) - simplified with better error handling
RUN curl -L https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-639.x86_64-unknown-linux.zip -o bento4.zip \
    && unzip -q bento4.zip Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt \
    && install -m 0755 Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt /usr/local/bin/mp4decrypt \
    && rm -rf Bento4-SDK-1-6-0-639.x86_64-unknown-linux bento4.zip

# Create MP4Box symlink if needed
RUN which MP4Box || which mp4box || ln -sf /usr/bin/MP4Box /usr/local/bin/mp4box 2>/dev/null || echo "MP4Box tools installed"

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies with better error handling
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir gamdl \
    && pip install --no-cache-dir beets[web] \
        beets-extrafiles \
        requests \
        beautifulsoup4 \
        pylast \
    && pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers with error handling
RUN playwright install chromium \
    && playwright install-deps chromium

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