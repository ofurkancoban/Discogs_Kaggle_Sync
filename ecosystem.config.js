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
//
// The real, rotating log is logs/sync.log, written directly by the script itself
// (discogs_kaggle_sync/logging_setup.py) - it keeps 14 days of history on its own. pm2's
// own out_file/error_file below are just its usual raw stdout/stderr capture and are NOT
// rotated by pm2 itself; if you want those bounded too, run
// `pm2 install pm2-logrotate` once (a separate pm2 module, not something this project
// manages), or just rely on logs/sync.log and ignore pm2's copies.

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
      out_file: "./logs/pm2-sync.out.log",
      error_file: "./logs/pm2-sync.err.log",
      time: true,
    },
    {
      // Optional: catch drift between "marked published" and "actually complete on
      // Kaggle" (see fill_descriptions.py --audit) once a week.
      name: "discogs-kaggle-audit",
      script: "fill_descriptions.py",
      interpreter: "./.venv/bin/python3",
      args: "--kaggle-owner ofurkancoban --audit",
      cron_restart: "0 7 * * 1",
      autorestart: false,
      watch: false,
      out_file: "./logs/pm2-audit.out.log",
      error_file: "./logs/pm2-audit.err.log",
      time: true,
    },
  ],
};
