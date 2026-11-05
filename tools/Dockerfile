# 工具服务镜像：node + git（提交钩子要用）+ mysql2
FROM node:20-alpine

# git 是硬需求：文书提交要跑 commit-msg / pre-commit 钩子
RUN apk add --no-cache git bash tzdata \
 && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY src ./src

# 运行身份：官方镜像自带 node(uid 1000)，与宿主 baize 对齐，工作区文件权限不会打架
RUN mkdir -p /ws /blobs && chown -R node:node /ws /blobs /app

USER node

ENV NODE_ENV=production \
    WS_ROOT=/ws \
    MYSQL_HOST=jl-mysql \
    MYSQL_PORT=3306 \
    MYSQL_DATABASE=jl

ENTRYPOINT ["node", "/app/src/server.js"]
