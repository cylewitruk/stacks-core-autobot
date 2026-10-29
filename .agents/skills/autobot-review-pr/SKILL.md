---
name: autobot-review-pr
description: Review the exact published Autobot PR head independently.
---

# autobot-review-pr

For standalone use, first read the [shared instructions](../../../.autobot/instructions.md) and [versioned project charter](../../../.autobot/charter.json). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review complete before/after files, acceptance criteria and applicable repository review guidance. Peer votes are withheld; your own prior findings are evidence. Approve only without findings. Request changes for implementation defects. Signal requirements or plan defects using verdict reopen and structured design_issue evidence; fresh unanimous design approval is required before further changes.

In the pipeline, use the supplied external [response contract](../../../.autobot/schemas/review.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
