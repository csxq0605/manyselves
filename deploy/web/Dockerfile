FROM node:22-alpine AS builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend ./
COPY frontend-contract /app/frontend-contract
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY --from=builder /app/frontend/dist /usr/share/nginx/html
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
EXPOSE 9090
