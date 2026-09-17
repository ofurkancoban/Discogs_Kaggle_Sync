// Alternative to crontab.example: run the sync under pm2 instead of plain cron.
//
// pm2 can schedule any executable via `cron_restart`, not just Node processes - this
// runs run_monthly_sync.py once a day and relies on the same idempotency check (it exits
// immediately on days there's nothing new). Useful if the VPS already uses pm2 for other
// processes, since you get `pm2 logs`, `pm2 monit`, and `pm2 status` for this job too
// instead of a separate crontab entry to remember.
//
// Setup:
//   npm install -g pm2
//   pm2 start ecosystem.config.js
//   pm2 save                 # persist across reboots
//   pm2 startup              # (follow its printed instructions once, as root/sudo)
//
// Adjust `interpreter` to your venv's python and `args` to your Kaggle username first.

module.exports = {
  apps: [
    {
      name: "discogs-kaggle-sync",
      script: "run_monthly_sync.py",
      interpreter: "./.venv/bin/python3",
      args: "--work-dir ./work --kaggle-owner ofurkancoban",
      cron_restart: "0 6 * * *",
      autorestart: false, // one-shot per cron tick, not a long-running service
      watch: false,
      out_file: "./logs/sync.out.log",
      error_file: "./logs/sync.err.log",
      time: true,
    },
  ],
};
