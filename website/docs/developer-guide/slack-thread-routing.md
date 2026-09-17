# Slack thread routing controls

Hermes Slack gateway supports a per-thread routing control mode for channels that
normally require an explicit bot mention.

## Purpose

The feature lets a user silence the bot in a Slack thread with a configured stop
phrase, then resume normal auto-follow behavior with a configured resume phrase.
It is meant for active agent threads where the bot should stop reacting to
follow-up human discussion unless directly mentioned again.

## Config

The settings live under the Slack platform config:

```yaml
slack:
  require_mention: true
  thread_routing:
    enabled: true
    state_file: slack/thread_routing_state.json
    suppress_other_user_mentions: true
    stop_patterns:
      - '(?<!\w)не\s+отвечай(?!\w)'
      - '\bstop\s+(responding|replying)\b'
    resume_patterns:
      - '(?<!\w)можешь\s+(снова\s+|опять\s+)?отвечать(?!\w)'
      - '\bresume\s+(responding|replying)\b'
```

`stop_patterns` and `resume_patterns` are Python regular expressions evaluated
case-insensitively against the raw Slack text. Bot mentions remain in the raw
text during control matching, so a phrase such as `<@BOT> не отвечай на это
сообщение` can match `не отвечай`.

## Behavior

When `thread_routing.enabled` is true:

- A stop phrase in a direct bot mention sets that team/channel/thread to
  `strict_mention` and suppresses model invocation for that control message.
- A stop phrase without a bot mention is accepted only when the thread is already
  an active agent thread in the same Slack team/workspace. Otherwise it is ignored
  as a normal inactive-channel message.
- While a thread is strict, unmentioned messages are ignored. Direct bot mentions
  still route to the model.
- A resume phrase only clears strict mode when the bot is directly mentioned.
  Unmentioned resume text does not wake a silent thread.
- Messages that mention another Slack user inside an active agent thread are
  suppressed by default unless the bot is also mentioned. This is controlled by
  `suppress_other_user_mentions`.

The feature intentionally does not send text acknowledgements for stop/resume.
Stop/resume are control-plane commands: stop is silent, and direct resume follows
the normal mention handling path. If product requirements change, add explicit
ack settings as a separate backward-compatible config option rather than making
ack messages mandatory.

## Team/workspace scoping

Thread routing state is scoped by Slack team/workspace, channel, and thread:

```text
{team_id}:{channel_id}:{thread_ts}
```

Session-backed active-thread eligibility is also team-aware. A session entry is
trusted for unmentioned auto-follow/stop only when `entry.origin.guild_id` equals
the event `team_id`. Legacy Slack sessions without `origin.guild_id` do not grant
silent auto-follow/stop privileges when the event is team-aware.

This prevents a thread in one Slack workspace from enabling silent routing or stop
controls in another workspace that happens to reuse the same channel/thread IDs.

## State file

Strict-mode state is persisted in `state_file`, relative to `HERMES_HOME` unless
an absolute path is configured. Mutations hold a process-local lock and a POSIX
advisory file lock, then write through a unique temporary file and atomically
replace the state file. A sibling `.pending` transaction marker plus file and
directory `fsync` calls make incomplete or ambiguous writes fail closed after a
restart.

The lock is host-local. Multiple gateway processes on the same POSIX host may
share the file, but deployments spanning hosts should use a shared transactional
backend rather than a shared filesystem whose lock and durability semantics have
not been verified. Platforms without `fcntl` fail closed instead of mutating
state without a lock.

## Tests

Focused tests live in:

```text
tests/gateway/test_slack_thread_routing.py
```

Useful checks:

```bash
scripts/run_tests.sh tests/gateway/test_slack_thread_routing.py -q
scripts/run_tests.sh tests/gateway/test_slack.py tests/gateway/test_slack_mention.py tests/gateway/test_slack_thread_routing.py -q
```
