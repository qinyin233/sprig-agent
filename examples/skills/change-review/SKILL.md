---
name: change-review
description: Review a proposed coding change against its requirements and produce evidence-backed findings. Use for code review or checking that a fix covers the reported bug.
license: MIT
compatibility: Sprig file tools; commands require separate user approval.
metadata:
  version: "1"
---

# Change review

Establish the requested behavior and the scope of the proposed change. Read the applicable project guidance and the relevant code before drawing conclusions. If the requirement is materially ambiguous, ask the user a focused question.

Trace changed behavior through its callers and error paths. Check cancellation, malformed input, resource lifetime, and platform differences where relevant. Read tests to learn what they actually verify; passing tests alone do not prove a broad product claim.

Read [the report template](references/report-template.md) when preparing findings. Tie each actionable finding to a concrete trigger, consequence, and source location. Distinguish observed behavior from inference. Avoid speculative warnings without an affected path.

For a review-only request, report findings without editing files. For an authorized fix, propose the smallest coherent correction, obtain the application's fresh approval for each write or command, and validate the affected behavior. Do not change acceptance checks merely to produce a passing result.

Loading this skill does not run scripts or approve any operation. Existing user instructions and application restrictions remain in force.
