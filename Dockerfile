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

# Build MP4Box from source (working method)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cmake \
        make \
        gcc \
        g++ \
        zlib1g-dev \
        libjpeg-dev \
        libpng-dev \
        libfreetype6-dev \
        libssl-dev \
        libxml2-dev \
    && git clone --depth 1 https://github.com/gpac/gpac.git /tmp/gpac \
    && cd /tmp/gpac \
    && ./configure --static-mp4box --enable-static-bin \
    && make -j$(nproc) \
    && make install \
    && cd / \
    && rm -rf /tmp/gpac \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && echo "MP4Box built from source successfully" \
    && MP4Box -version

# Install mp4decrypt (Bento4)
RUN curl -L https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-639.x86_64-unknown-linux.zip -o bento4.zip \
    && unzip -q bento4.zip Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt \
    && install -m 0755 Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt /usr/local/bin/mp4decrypt \
    && rm -rf Bento4-SDK-1-6-0-639.x86_64-unknown-linux bento4.zip \
    && echo "MP4 decryption tools installed"

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

# Install Playwright browsers and required system deps (Debian packages)
RUN playwright install chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libxss1 \
        libasound2 \
        fonts-unifont \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

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