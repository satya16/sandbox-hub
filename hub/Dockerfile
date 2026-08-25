FROM node:20-slim AS ui-build
WORKDIR /ui
COPY hub-ui/package.json hub-ui/package-lock.json ./
RUN npm ci --silent
COPY hub-ui/ .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY hub/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hub/app ./app
COPY --from=ui-build /ui/dist ./static
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
