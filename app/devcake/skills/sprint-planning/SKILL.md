---
name: sprint-planning
description: Plan a sprint — scope work, estimate capacity, set goals, and draft a sprint plan. Use when kicking off a new sprint, sizing a backlog against team availability (accounting for PTO and meetings), deciding what's P0 vs. stretch, or handling carryover from the last sprint.
license: Apache-2.0
metadata:
  source: https://github.com/anthropics/knowledge-work-plugins
  author: Anthropic
---

# Sprint Planning

Plan a sprint by scoping work, estimating capacity, and setting clear goals. Works standalone from information the user provides; if a project tracker export or backlog dump is available, use it as the source for backlog items.

## Inputs to Gather

Ask for what's missing — most important first, conversationally:

- **Team**: Who's on the team and their availability this sprint?
- **Sprint length**: How many days/weeks?
- **Backlog**: What's prioritized? (Paste from the tracker or describe)
- **Carryover**: Anything unfinished from last sprint?
- **Dependencies**: Anything blocked on other teams?

## Workflow

1. **Define the sprint goal** — one clear sentence about what success looks like. If it can't be stated in one sentence, the sprint is unfocused; help the user narrow it.
2. **Estimate capacity** — per person: working days minus PTO, on-call, meetings, and interviews. Sum to team capacity in the team's unit (points, days, or hours).
3. **Scope the backlog** — assign each candidate item a priority (P0 must ship, P1 should ship, P2 stretch), an estimate, and an owner. Include carryover items only after understanding why they slipped.
4. **Check the load** — compare sprint load against capacity. Plan to 70-80% of capacity; flag overcommitment explicitly.
5. **Identify risks and dependencies** — what could blow up the plan, what happens if it does, and the mitigation.
6. **Produce the sprint plan document** using the template below.

## Output

```markdown
## Sprint Plan: [Sprint Name]
**Dates:** [Start] — [End] | **Team:** [X] engineers
**Sprint Goal:** [One clear sentence about what success looks like]

### Capacity
| Person | Available Days | Allocation | Notes |
|--------|---------------|------------|-------|
| [Name] | [X] of [Y] | [X] points/hours | [PTO, on-call, etc.] |
| **Total** | **[X]** | **[X] points** | |

### Sprint Backlog
| Priority | Item | Estimate | Owner | Dependencies |
|----------|------|----------|-------|--------------|
| P0 | [Must ship] | [X] pts | [Person] | [None / Blocked by X] |
| P1 | [Should ship] | [X] pts | [Person] | [None] |
| P2 | [Stretch] | [X] pts | [Person] | [None] |

### Planned Capacity: [X] points | Sprint Load: [X] points ([X]% of capacity)

### Risks
| Risk | Impact | Mitigation |
|------|--------|------------|
| [Risk] | [What happens] | [What to do] |

### Definition of Done
- [ ] Code reviewed and merged
- [ ] Tests passing
- [ ] Documentation updated (if applicable)
- [ ] Product sign-off

### Key Dates
| Date | Event |
|------|-------|
| [Date] | Sprint start |
| [Date] | Mid-sprint check-in |
| [Date] | Sprint end / Demo |
| [Date] | Retro |
```

## Tips

1. **Leave buffer** — Plan to 70-80% capacity. You will get interrupts.
2. **One clear sprint goal** — If you can't state it in one sentence, the sprint is unfocused.
3. **Identify stretch items** — Know what to cut if things take longer than expected.
4. **Carry over honestly** — If something didn't ship, understand why before re-committing.

---
*Adapted from [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) (Apache-2.0).*
