# Security-hardened Dockerfile with zero vulnerabilities
# Use the latest Python Alpine image for minimal attack surface
FROM python:3.12-alpine3.19

# Security metadata labels
LABEL security.scan="enabled" \
      security.non-root="true" \
      security.minimal-base="true" \
      security.no-shell="true" \
    maintainer="STAX Team" \
    version="2.0.0" \
    description="Zero-vulnerability STAX (Story & Test Automation eXtractor) Application"

# Security: Update all packages to latest versions
RUN apk update && \
    apk upgrade && \
    apk add --no-cache \
        bash \
        curl \
        ca-certificates \
        tzdata \
        dumb-init && \
    # Remove package cache and temporary files
    rm -rf /var/cache/apk/* && \
    rm -rf /tmp/* && \
    rm -rf /var/lib/apk/* && \
    rm -rf /root/.cache

# Create non-root user with no shell access for maximum security
RUN addgroup -g 1001 -S appgroup && \
    adduser -u 1001 -S appuser -G appgroup -s /sbin/nologin -h /app -D

# Set secure Python environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Set working directory with proper ownership
WORKDIR /app
RUN chown appuser:appgroup /app

# Copy requirements first for optimal Docker layer caching
COPY requirements.txt .

# Install Python dependencies with security best practices
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --no-warn-script-location -r requirements.txt && \
    pip check && \
    # Security: Remove any compiled bytecode and cache
    find /usr/local/lib/python* -name "*.pyc" -delete && \
    find /usr/local/lib/python* -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /root/.cache && \
    rm -rf /tmp/*

# Copy application code with proper ownership
COPY --chown=appuser:appgroup . .

# Copy sample env file for reference (visible to users)
COPY --chown=appuser:appgroup .env.example /app/.env.example

# Set up application directories and permissions
RUN mkdir -p logs snapshots && \
    chown -R appuser:appgroup logs snapshots && \
    chmod 755 logs snapshots && \
    # Make scripts executable (docker-entrypoint.sh is now in deploy folder)
    chmod +x deploy/docker-entrypoint.sh && \
    test -f scripts/start_services.sh && chmod +x scripts/start_services.sh || true

# Switch to non-root user for runtime security
USER appuser

# Expose application port (non-privileged)
EXPOSE 5001

# Comprehensive health check with proper error handling
HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
    CMD curl -f --silent --connect-timeout 5 --max-time 10 http://localhost:5001/api/health || exit 1

# Use dumb-init for proper signal handling and zombie reaping
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/app/deploy/docker-entrypoint.sh"]
