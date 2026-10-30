# Reviewed changes and upstream PRs

Upstream inspected: `98b68ef82a47690c316f42fddb05baafaab56851` on `main`. Current open PRs/issues were reviewed for deployment, initialization and judging impact. The OWL harness PR was unrelated and not applied. Existing fixes were reviewed and cherry-picked with original author attribution. No upstream PR was merged.

| Change | Deployment commits | Upstream PR |
| --- | --- | --- |
| Correct meeting-room reference from 4 to 5, matching five overlapping meetings | `a239d14` | [#1098](https://github.com/TheAgentCompany/TheAgentCompany/pull/1098), existing fix |
| Add missing Rocket.Chat task dependency | `7766f71` | [#1094](https://github.com/TheAgentCompany/TheAgentCompany/pull/1094), existing fix |
| Bound crashed-server evaluator request | `dd9be48`, `55d069a` | [#1103](https://github.com/TheAgentCompany/TheAgentCompany/pull/1103) |
| Propagate controller reset failures; log background errors | `d5896cf` | [#1104](https://github.com/TheAgentCompany/TheAgentCompany/pull/1104) |
| Resolve Plane downloads without Git metadata; preserve config on failure | `d3a5847` | [#1105](https://github.com/TheAgentCompany/TheAgentCompany/pull/1105) |
| Reject incidental digits and recipient replies in room-count messages | `4640f78` | [#1106](https://github.com/TheAgentCompany/TheAgentCompany/pull/1106) |
| Structured Codex CLI judging; propagate judge failures | `9daadce`, `b17cb9c` | [#1107](https://github.com/TheAgentCompany/TheAgentCompany/pull/1107) |
| Package the exact official MinIO release after registry removal | `655960f` | [#1108](https://github.com/TheAgentCompany/TheAgentCompany/pull/1108) |
| Align seven task input paths with existing supplied files | `f277a4d`, `6a0f194` | [#1109](https://github.com/TheAgentCompany/TheAgentCompany/pull/1109) |
| Fail initialization on unsuccessful reset HTTP responses | `4eea6bd` | [#1110](https://github.com/TheAgentCompany/TheAgentCompany/pull/1110) |
| Restore five original missing inputs; correct folder permissions/indexing | `d63ce05`, `075553a` | [#1111](https://github.com/TheAgentCompany/TheAgentCompany/pull/1111) |
| Report ownCloud download/infrastructure failures without false success | `da7dac6` | [#1112](https://github.com/TheAgentCompany/TheAgentCompany/pull/1112) |
| Reject malformed spreadsheet matcher output instead of granting credit | `3c75e5e`, `a28a4a6` | [#1113](https://github.com/TheAgentCompany/TheAgentCompany/pull/1113) |
| Renew expired existing GitLab fixture; authenticated readiness | `4ca7af1` | [#1114](https://github.com/TheAgentCompany/TheAgentCompany/pull/1114) |

The reference-answer correction is an explicit scoring-defect repair. Other checkpoint weights, reference tables, predicate wording and score aggregation were preserved. Infrastructure failures now stop evaluation where changed, rather than appearing as a Viv failure or negative score. Twelve new upstream PRs are independently based on the inspected upstream revision; equivalent existing PRs were not duplicated.

Deployment-specific Compose/resource settings, preservation/recovery scripts, recorded verification and the existing private HTTPS configuration are maintained in this fork. Coding-task execution is excluded at the user's request. The MinIO repair uses a distinct local image name and the exact pinned binary/version; it does not substitute another S3 implementation.
