# Scheduling

The plists use `/path/to/investimo` as a placeholder. Substitute your checkout
while copying:

```sh
for f in launchd/*.plist; do
  sed "s#/path/to/investimo#$PWD#g" "$f" > ~/Library/LaunchAgents/"$(basename "$f")"
done
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investimo.daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investimo.bot.plist
```

Check they're registered:

```sh
launchctl list | grep investimo
```

Logs land in `data/daily.log` and `data/bot.log`.

To reload after editing a plist:

```sh
launchctl bootout   gui/$(id -u) ~/Library/LaunchAgents/com.investimo.daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investimo.daily.plist
```

## The Mac has to be awake at 16:30

`launchd` will not wake a sleeping machine for a `StartCalendarInterval` job — it
runs the job late, on next wake. Schedule a wake a few minutes before:

```sh
sudo pmset repeat wakeorpoweron MTWRF 16:25:00
```

The plist time is local. If this machine isn't on US Eastern, convert 16:30 ET.
The scan needs 16:20 ET or later; before that it falls back to the previous
session, whose bar is final.

If the Mac sleeps through several sessions, launchd fires once on wake. That run
scans only the latest session, so touches on the skipped days are never
reported. A missed run shows up in `/status`.
