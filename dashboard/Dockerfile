FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:stable-alpine
COPY nginx-main.conf /etc/nginx/nginx.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY routes.conf /etc/nginx/agentos-routes.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER 101:101
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
EXPOSE 8080
