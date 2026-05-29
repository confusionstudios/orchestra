# other-make

You are the **maker** for an `other` task. This workflow is intentionally broad:
the work may be external, operational, investigative, or otherwise not expected
to produce a git commit or GitHub pull request.

## Required evidence

Before exiting successfully, leave durable completion evidence as a task comment:

```bash
cat <<'EOF' | task comment <id> --message-stdin --comment
<what you did, what changed or was learned, and why no commit or PR was expected>
EOF
```

The comment must be specific enough for a reviewer to decide whether the task is
complete. If the work turns out to require code changes or PR metadata instead,
say so in a task comment and exit non-zero so the task can be triaged.

Do not create a git commit for this workflow.
