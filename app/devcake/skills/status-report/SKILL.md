---
name: status-report
description: Generate a status report with KPIs, risks, and action items. Use when writing a weekly or monthly update for leadership, summarizing project health with green/yellow/red status, surfacing risks and decisions that need stakeholder attention, or turning a pile of project activity into a readable narrative.
license: Apache-2.0
metadata:
  source: https://github.com/anthropics/knowledge-work-plugins
  author: Anthropic
---

# Status Report

Generate a polished status report for leadership or stakeholders.

## Inputs to Gather

Ask the user for what's missing:

- **Scope**: Which project or team, and which period (weekly / monthly / quarterly)?
- **Progress**: What shipped or moved this period? What's in flight?
- **Metrics**: Which KPIs matter to this audience, with targets and actuals?
- **Risks and blockers**: What's threatening the plan?
- **Decisions needed**: What does the audience need to decide, and by when?

If the user can paste tracker exports, standup notes, or chat threads from the period, mine them for accomplishments, blockers, and decisions rather than asking the user to retype everything.

## Status Definitions

- **🟢 On Track** — plan holds; no stakeholder action needed.
- **🟡 At Risk** — plan holds only if named risks are mitigated; stakeholders should know now.
- **🔴 Off Track** — plan will not hold without intervention (scope, time, or people); a decision is required.

Rate each risk by impact (what happens if it lands) and always pair it with a mitigation and an owner — a risk without an owner is a prediction, not a plan.

## Output

```markdown
## Status Report: [Project/Team] — [Period]
**Author:** [Name] | **Date:** [Date]

### Executive Summary
[3-4 sentence overview — what's on track, what needs attention, key wins]

### Overall Status: 🟢 On Track / 🟡 At Risk / 🔴 Off Track

### Key Metrics
| Metric | Target | Actual | Trend | Status |
|--------|--------|--------|-------|--------|
| [KPI] | [Target] | [Actual] | [up/down/flat] | 🟢/🟡/🔴 |

### Accomplishments This Period
- [Win 1]
- [Win 2]

### In Progress
| Item | Owner | Status | ETA | Notes |
|------|-------|--------|-----|-------|
| [Item] | [Person] | [Status] | [Date] | [Context] |

### Risks and Issues
| Risk/Issue | Impact | Mitigation | Owner |
|------------|--------|------------|-------|
| [Risk] | [Impact] | [What we're doing] | [Who] |

### Decisions Needed
| Decision | Context | Deadline | Recommended Action |
|----------|---------|----------|--------------------|
| [Decision] | [Why it matters] | [When] | [What I recommend] |

### Next Period Priorities
1. [Priority 1]
2. [Priority 2]
3. [Priority 3]
```

## Tips

1. **Lead with the headline** — Busy leaders read the first 3 lines. Make them count.
2. **Be honest about risks** — Surfacing issues early builds trust. Surprises erode it.
3. **Make decisions easy** — For each decision needed, provide context and a recommendation.

---
*Adapted from [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) (Apache-2.0).*
