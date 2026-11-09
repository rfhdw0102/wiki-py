FROM mcr.microsoft.com/playwright:v1.58.2-noble

USER root
RUN node -e "if(Number(process.versions.node.split('.')[0])<22)process.exit(1)"
WORKDIR /app
COPY package.json package-lock.json ./
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force \
    && mkdir .runtime && chown pwuser:pwuser .runtime
COPY --chown=pwuser:pwuser src ./src
USER pwuser
EXPOSE 8080
CMD ["node", "src/index.js"]
