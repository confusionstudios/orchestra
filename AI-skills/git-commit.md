You will make a git commit.
- Read the files and ignore your memory of what changed.
- Git Commit Message Format:
   - **Title**: Use Title Case For Commit Message Titles. Synthetic. Less than 80 characters.
   - **Leading paragraph**: After the title, write one short paragraph about the main change.
   - **WHY** (optional): Add a `WHY` section when the problem being solved adds useful context. Describe the user pain, failure mode, or constraint — not the implementation again. Omit the heading when empty.
   - **WORK**: Add a `WORK` section with concise Unicode `•` bullets for the main implementation work.
   - **OTHER** (optional): Add an `OTHER` section only for related work that does not belong in the main story. Omit the heading when empty.
   - **NOTES** (optional): Add a `NOTES` section only for useful process detail, such as validation, migration, follow-up, or review context. Omit the heading when empty.
   - **Wording**: Prefer direct statements of what changed and why. Avoid contrastive filler like `instead of`, `rather than`, or `no longer` unless the comparison is the important point.
- SYNTHESIZE. The commit subject and opening sentences should tell the reader what changed at a high level and why.
- SUMMARIZE the purpose. Do NOT repeat the code changes line by line or enumerate the diff. We already have the code and file list.
- Only mention file paths if it adds value. The file changes are part of the commit.
- Do not mention Claude/Codex/Gemini as code author unless specifically asked. The human is the author. THERE IS NO CO-AUTHOR, Claude!
- Do NOT wait for confirmation. Just commit immediately.

## Example

Physics Engine: Knob Braking and Ramp Unified Under Base Class

Shared ramp and braking logic now live in `PhysicsEngineConcreteBase`.

WHY
• Divergent release behavior made braking inconsistent across control types.

WORK
• KnobSlider and XYPad now delegate to the same braking path.

OTHER
• Renamed `applyFriction` to `applyBraking` so the shared behavior reads the same way it behaves.
