# Morning and Night Meeting-Memory Sync Design

## Goal

Keep the Mac meeting-memory mirror current by pulling from the remote server in
the morning and after the nightly durable-knowledge job.

## Schedule

Run the existing `com.rz.meeting-memory-sync` launch agent every day at:

- 08:45 Asia/Singapore time, to catch overnight and previous-day output.
- 21:15 Asia/Singapore time, 25 minutes after the remote durable-knowledge job
  starts at 20:50.

Daily scheduling includes weekends so Friday-night output is available on
Saturday rather than waiting until Monday.

## Implementation

Reuse the existing sync wrapper and change only the launchd calendar intervals.
Keep the current logging, SSH configuration, rsync filters, and failure behavior.
Reload the launch agent after installing the updated property list.

## Verification

Validate the property list, reload it, and inspect launchd's active definition
to confirm exactly two daily triggers at 08:45 and 21:15. Run the wrapper once
manually to bring the local mirror current and confirm a successful log entry.
