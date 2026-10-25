# Image publication

The `Release` workflow publishes container images on pushes to `master`. It
checks out the triggering commit using the job's automatic `GITHUB_TOKEN` with
`contents: read` and does not persist Git credentials. A custom `GH_TOKEN` is
not needed for this workflow. Repository branch protections are unchanged.

The image is built once, labeled with its source SHA, and checked with
`pip check` before cloud credentials are configured. This is not a replacement
for the pull-request regression, HTTP and recovery tests.

## Registry contract

The three existing destinations remain in order:

| Credential secrets | Region |
| --- | --- |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `eu-west-1` |
| `AWS_ACCESS_KEY_ID_2`, `AWS_SECRET_ACCESS_KEY_2` | `ap-southeast-1` |
| `AWS_ACCESS_KEY_ID_3`, `AWS_SECRET_ACCESS_KEY_3` | `eu-west-1` |

Each credential pair selects its default private ECR registry. The workflow
does not provision repositories or grant IAM access. Existing credentials must
still permit ECR login and pushing to the `api` repository; their validity can
only be established by a successful authorized publication.

Each destination receives the full commit-SHA tag followed by `latest` for
backward compatibility. The job summary records the repository digest reported
by Docker after the SHA push, before attempting the mutable alias. Publication
stops at the first error; it is not atomic across tags or registries. Inspect
the failed step, logs and summary to identify partial publication. Missing a
receipt is not proof that nothing was uploaded, for example if the runner lost
connectivity after a push. There is no automatic deletion or rollback.

Runs are serialized without canceling an in-progress push. GitHub concurrency
may replace a pending run and does not promise FIFO ordering. Do not treat
`latest` as a deployment identifier. Re-running an older workflow can move the
alias backwards; review the source SHA before any rerun. Rebuilding a SHA is
not a guarantee of identical bytes (the base image is not digest-pinned).
Record and verify the registry digest before deployment.

## Versioning and deployment boundaries

Image publication no longer invokes the floating Python Semantic Release
action. It does not bump `setup.py`, write `CHANGELOG.md`, create Git tags or
GitHub releases, or push version commits onto protected `master`. The legacy
`setup.cfg` remains for reference, not as a validated automatic release path.
Any semantic-version release requires a separately reviewed compatible tool
and protected-branch process. Do not re-enable the old action by supplying a
personal token or weakening branch protections.

This workflow contains no deployment, database repair, migration or restart.
Updating registry tags does not change deployments pinned to another SHA or
digest, but consumers following `latest` can pick up the new image. Before
deploying, pull the selected digest, verify its source label and smoke-test the
actual artifact. Follow the separate [backup and rollout procedure](backup-and-rollout.md).

## Local checks

Build the existing test image, then run publication checks without a network,
cloud credentials, a Docker socket or a database:

```bash
docker build -f tests/Dockerfile -t api-cache-tests .
docker run --rm --network none \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  --entrypoint python api-cache-tests \
  -m unittest discover -s tests/workflows -v
```

These checks parse the workflow and execute the publication script with a fake
Docker command, including failures at each call. They verify local control
flow, not GitHub/AWS authorization or a real registry upload. CI also runs the
application integration suite and image smoke tests.
