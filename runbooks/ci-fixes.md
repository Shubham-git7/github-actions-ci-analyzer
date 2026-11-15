# CI fix runbook (sample)

## Missing build script
When npm reports `missing script: build`, add `"build"` to `scripts` in package.json. Only package.json may change.

## Unpinned dependency breaks install
When pip or npm install fails because a new upstream release broke compatibility, pin the previous working version in requirements.txt or package.json. Never loosen pins.

## Deprecated action version
When a workflow fails because an action version is deprecated, bump it to the latest major in .github/workflows/*.yml. Never replace an action with an unreviewed third-party action.
