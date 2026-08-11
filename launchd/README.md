# Scheduling

```sh
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.investimo.daily.plist
launchctl load ~/Library/LaunchAgents/com.investimo.bot.plist
```

Check they're registered:

```sh
launchctl list | grep investimo
```

Logs land in `data/daily.log` and `data/bot.log`.

To reload after editing a plist:

```sh
launchctl unload ~/Library/LaunchAgents/com.investimo.daily.plist
launchctl load   ~/Library/LaunchAgents/com.investimo.daily.plist
```

## The Mac has to be awake at 16:30

`launchd` will not wake a sleeping machine for a `StartCalendarInterval` job — it
runs the job late, on next wake. Schedule a wake a few minutes before:

System Settings → General → Login Items & Extensions → Wake for network access,
or `sudo pmset repeat wakeorpoweron MTWRF 16:25:00`.

If a run is missed entirely, `/status` in Telegram will say so — it flags when the
last successful run predates the most recent trading session.
