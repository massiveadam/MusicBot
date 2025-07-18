FROM python:3.12-slim

# Install system dependencies including MP4Box
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
    gpac \
    && rm -rf /var/lib/apt/lists/*

# Build and install MP4Box (GPAC)
RUN git clone https://github.com/gpac/gpac.git /usr/local/src/gpac && \
    cd /usr/local/src/gpac && \
    ./configure --static-bin && \
    make -j$(nproc) && \
    make install && \
    rm -rf /usr/local/src/gpac

# Install mp4decrypt (Bento4)
RUN set -eux; \
    curl -L https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-639.x86_64-unknown-linux.zip -o bento4.zip && \
    unzip -q bento4.zip Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt && \
    install -m 0755 Bento4-SDK-1-6-0-639.x86_64-unknown-linux/bin/mp4decrypt /usr/local/bin/mp4decrypt && \
    rm -rf Bento4-SDK-1-6-0-639.x86_64-unknown-linux bento4.zip

# Create MP4Box symlink (gpac package installs it as 'MP4Box')
RUN ln -sf /usr/bin/MP4Box /usr/local/bin/mp4box || echo "MP4Box already available"

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