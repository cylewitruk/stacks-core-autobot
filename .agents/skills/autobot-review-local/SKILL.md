---
name: autobot-review-local
description: Review an Autobot candidate and real local check evidence before publication.
---

# autobot-review-local

For standalone use, first read the [shared instructions](../../../.autobot/instructions.md) and [versioned project charter](../../../.autobot/charter.json). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review complete before/after source and real local checks. Approve only without findings. Request concrete fixes for implementation defects. Use verdict reopen plus structured design_issue evidence for a requirements or plan defect; do not force implementation beyond the approved design. Missing source or coverage requires findings.

In the pipeline, use the supplied external [response contract](../../../.autobot/schemas/local_review.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
