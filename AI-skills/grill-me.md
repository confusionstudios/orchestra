Stress-test a plan, design, decision, or idea with focused interview rounds, then summarize the agreed decisions before implementation.

Use this when the user wants to grill a plan, design, decision, or idea, or asks to be interviewed before building. Run the interview yourself. Do not call another skill, and do not depend on an upstream skill name being installed.

The interview is a discussion. Starting it does not authorize implementation.

## Decision dependency tree

Treat the plan as a tree of decisions. A decision can unblock later decisions that depend on it. The frontier is every decision whose prerequisites are already settled: the questions you can ask now without guessing an answer you have not heard.

Work the tree in rounds:

1. Recompute the frontier from the settled decisions and the user's latest reply.
2. Ask the current useful frontier in one numbered round. Give a recommended answer for each question.
3. Stop and wait for the user. Do not ask the next round in the same turn.
4. After the user answers, update the tree and recompute the frontier.

A question that depends on another question still open in this round belongs in a later round. Also leave out branches that are irrelevant, premature, or only useful if an unsettled choice goes a particular way. If the useful frontier is still large, ask the highest-leverage independent questions and briefly say what you deferred.

Word each question so "yes" accepts the recommended answer when that is practical. When a decision is not a yes/no, lead with the recommended choice and say what agreeing to it means.

Use this shape:

```text
**Q1. <short title>**

<question. Include the real options and the tradeoff when they matter.>

Recommended answer: <answer>

---

**Q2. <short title>**

<question>

Recommended answer: <answer>
```

## Facts and decisions

Find environment facts yourself from the evidence and tools available. Do not ask the user for something you can inspect, such as a file, a command result, a config value, or the current repo. If you cannot find a fact, say what you looked for, then ask.

Use the tools you already have. Follow the delegation rules you already follow. Do not launch a subagent solely because a fact needs looking up.

A lookup that is still running is an unsettled prerequisite. Ask the independent questions now, and hold only the questions that depend on that fact.

The decisions belong to the user. State assumptions, tradeoffs, and unresolved choices. Do not guess a missing decision and then treat the guess as agreed.

## Steering

Follow the user's scope. If they narrow the topic, skip a branch, or tell you to stop, do that. Leave the dropped branch out of later rounds unless they ask to reopen it.

## Agreed summary

Continue until the relevant frontier is empty: the decisions that matter are settled, and nothing important is still assumed in silence. Then give a concise summary:

- Decided points
- Assumptions that remain explicit
- Tradeoffs the user accepted
- Anything deliberately left unresolved

Do not implement, edit project files, or start the work during the interview. After the summary, get the user's go-ahead before implementation. If the user has already explicitly authorized implementation of the agreed decisions, deliver the summary and continue without asking for that permission again. A correction to the summary is new steering.

Do not mention this skill's sources, license, or attribution in questions, summaries, or other replies.

Upstream revision `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`; MIT notice: [notices/mattpocock-skills-NOTICE.txt](notices/mattpocock-skills-NOTICE.txt).

Adapted from Matt Pocock's skills: https://github.com/mattpocock/skills
