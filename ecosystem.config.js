module.exports = {
  apps: [
    {
      name: "upload-fastapi-server",
      script: "python",
      args: "-m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 30 --backlog 2048",
      cron_restart: "*/5 * * * *", // Auto-restart every 5 minutes
      max_memory_restart: "500M",
      autorestart: true,
      restart_delay: 1000,
      env: {
        PYTHONUNBUFFERED: "1",
        PORT: "8000",
        HOST: "0.0.0.0"
      }
    }
  ]
};
