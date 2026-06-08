# stash recovery: `stash_ref` is set

This section runs before the normal `commit-make` build or finalization prompt.
A prior `commit-make` run blocked after saving WIP with `git stash`. Restore
that work first.

1. **Try to restore the stash.** Run `git stash pop <stash_ref>`.

2. **If the pop is clean** — clear the recorded stash ref and continue with
   the normal prompt on the restored worktree:
   ```
   task set <id> --stash-ref ""
   ```

3. **If the pop conflicts** — drop the stash, clear the ref, and rebuild
   from scratch using the now-answered blocker details in task comments so
   you do not get stuck on the same question again:
   ```
   git stash drop <stash_ref>
   task set <id> --stash-ref ""
   ```

4. **If the stash ref is missing or invalid** — clear the ref, leave a
   durable comment explaining the missing stash entry, then rebuild from
   scratch:
   ```
   task set <id> --stash-ref ""
   cat <<'EOF' | task comment <id> --message-stdin --comment
   Stash <stash_ref> was missing or invalid; rebuilding from scratch.
   EOF
   ```
