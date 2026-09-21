FROM node:20-alpine AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM python:3.11-slim AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY attendance_app ./attendance_app
COPY run_api.py .
COPY --from=frontend /app/dist ./dist
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "attendance_app.api:app", "--host", "0.0.0.0", "--port", "8000"]
