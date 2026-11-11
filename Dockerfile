# Build the dashboard with Node, then run the application on Python.
# Cloady detects this root Dockerfile before Railpack language auto-detection.

FROM node:24-bookworm-slim AS frontend-build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000

COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY guide/ ./guide/
COPY --from=frontend-build /app/dist ./backend/dist/

EXPOSE 3000

CMD ["python", "-m", "backend.main"]
