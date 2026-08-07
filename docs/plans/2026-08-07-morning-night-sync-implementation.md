# Morning and Night Meeting-Memory Sync Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Pull the remote meeting-memory mirror every day at 08:45 and 21:15 Asia/Singapore time.

**Architecture:** Keep the existing launch agent and sync wrapper. Replace its weekday hourly calendar array with two daily calendar entries, install the validated plist, reload the agent, and manually run the wrapper once to catch up immediately.

**Tech Stack:** macOS launchd property lists, Bash, rsync, SSH

---

### Task 1: Stage and update the launch agent property list

**Files:**
- Modify: `/Users/rz/AI-native/meeting-logs/.local-sync/com.rz.meeting-memory-sync.plist`
- Modify: `/Users/rz/Library/LaunchAgents/com.rz.meeting-memory-sync.plist`

**Step 1: Copy the maintained plist to a writable staging path**

Run:

```bash
cp /Users/rz/AI-native/meeting-logs/.local-sync/com.rz.meeting-memory-sync.plist /private/tmp/com.rz.meeting-memory-sync.plist
```

Expected: the staging file exists and matches the maintained plist.

**Step 2: Replace the calendar intervals**

Set `StartCalendarInterval` to exactly:

```xml
<array>
  <dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>45</integer>
  </dict>
  <dict>
    <key>Hour</key>
    <integer>21</integer>
    <key>Minute</key>
    <integer>15</integer>
  </dict>
</array>
```

Omitting `Weekday` makes both triggers daily.

**Step 3: Validate the staged plist**

Run:

```bash
plutil -lint /private/tmp/com.rz.meeting-memory-sync.plist
plutil -p /private/tmp/com.rz.meeting-memory-sync.plist
```

Expected: lint reports `OK`, and the parsed schedule contains only 08:45 and 21:15.

**Step 4: Install both maintained and active copies**

Run:

```bash
cp /private/tmp/com.rz.meeting-memory-sync.plist /Users/rz/AI-native/meeting-logs/.local-sync/com.rz.meeting-memory-sync.plist
cp /private/tmp/com.rz.meeting-memory-sync.plist /Users/rz/Library/LaunchAgents/com.rz.meeting-memory-sync.plist
```

Expected: both copies match the validated staging file.

### Task 2: Reload and verify launchd

**Step 1: Reload the launch agent**

Run:

```bash
launchctl bootout gui/501/com.rz.meeting-memory-sync
launchctl bootstrap gui/501 /Users/rz/Library/LaunchAgents/com.rz.meeting-memory-sync.plist
```

Expected: the agent reloads without an error.

**Step 2: Verify the active schedule**

Run:

```bash
launchctl print gui/501/com.rz.meeting-memory-sync
```

Expected: exactly two calendar triggers are active, at 08:45 and 21:15 daily.

### Task 3: Pull current remote output

**Step 1: Run the existing wrapper once**

Run:

```bash
/Users/rz/AI-native/meeting-logs/.local-sync/run_launchd_meeting_memory_sync.sh
```

Expected: the sync log ends with `Finished meeting memory sync`.

**Step 2: Verify catch-up**

Compare local and remote knowledge-object counts and inspect the newest local knowledge timestamp.

Expected: local knowledge includes the partial output produced by the remote 2026-08-07 run; any remaining extraction gap is attributable to the separately diagnosed invalid-JSON source failure.
