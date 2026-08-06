FROM node:20-slim

WORKDIR /app

# Copy the lockfile too and use `npm ci` (not `npm install`) so the image
# always gets the exact, previously-resolved dependency tree instead of
# silently re-resolving on every build.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host"]