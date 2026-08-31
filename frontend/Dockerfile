FROM public.ecr.aws/docker/library/node:20-slim

WORKDIR /app

# Copy the lockfile too and use `npm ci` (not `npm install`) so the image
# always gets the exact, previously-resolved dependency tree instead of
# silently re-resolving on every build.
COPY package.json package-lock.json ./
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN npm install -g npm@latest
RUN npm ci

COPY . .

ENV VITE_API_BASE_URL=https://netguard-api.notoriousdev.in/api/v1

EXPOSE 6001

CMD ["npm", "run", "dev", "--", "--host"]